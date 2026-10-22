//! Native pre-dispatch workflow starts and frozen graph leases. Acquisition
//! leases bind execution custody and freshness; guarded deployment additionally
//! requires native approval. No lease authorizes unaccounted auxiliary dispatch.

use agentc_core::storage::{canonical_json, content_hash};
use anyhow::{bail, Context, Result};
use rusqlite::{params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};

use crate::multi_call::{CallPosition, MultiCallRuntimeBranch};
use crate::plan_profile::{PlanProfileKey, PlanRuntimeVersion};
use crate::provider_graph::{self, Execution, SourceContract, Spec};
use crate::provider_graph_quality::Policy;

pub const VERSION: &str = "provider-graph-preparation-v1";
type QualityHead = (String, i64, Option<i64>, Option<i64>, i64);

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Template {
    pub execution_id: String,
    pub spec: Spec,
    pub runtime_branches: Vec<MultiCallRuntimeBranch>,
    pub source_contracts: Vec<SourceContract>,
    pub head_sha256: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ComparisonRole {
    Reference,
    Candidate,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ComparisonContext {
    pub source_pair_id: String,
    pub source_pair_sha256: String,
    pub role: ComparisonRole,
    pub generation: crate::provider_graph_quality::Generation,
    pub generation_id: String,
    pub candidate_head_sha256: String,
    pub reference_head_sha256: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Lease {
    pub version: String,
    pub lease_id: String,
    pub batch_id: String,
    pub metadata_sha256: String,
    pub max_workers: u32,
    pub input_sha256: Option<String>,
    pub started_at_us: i64,
    pub policy: Policy,
    pub window_size: usize,
    pub template: Option<Template>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub comparison_context: Option<ComparisonContext>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub acquisition_version: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub role_program_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub role_comparison_context: Option<ComparisonContext>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub deployment_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub sampling_id: Option<String>,
}

pub(crate) enum Authorization {
    Acquisition,
    Deployment(String),
    Sampling(String),
    SamplingDeployment {
        sampling: String,
        deployment: String,
    },
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Begin {
    pub batch_id: String,
    pub max_workers: u32,
    pub input_sha256: Option<String>,
    pub template_execution_id: Option<String>,
    #[serde(default)]
    pub comparison_pair_id: Option<String>,
    #[serde(default)]
    pub role_program: Option<crate::provider_graph_roles::Prepared>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Node {
    pub position: CallPosition,
    pub key: PlanProfileKey,
    pub plan: crate::execution_plan::ExecutionPlanSpec,
    pub runtime: PlanRuntimeVersion,
    pub source_contract_sha256: String,
    pub logical_request_sha256: String,
}

pub fn ensure_schema(conn: &Connection) -> Result<()> {
    provider_graph::ensure_schema(conn)?;
    conn.execute_batch("CREATE TABLE IF NOT EXISTS provider_graph_preparation (
        lease_id TEXT PRIMARY KEY,batch_id TEXT NOT NULL UNIQUE,lease_sha256 TEXT NOT NULL,lease_json TEXT NOT NULL,
        execution_id TEXT UNIQUE,FOREIGN KEY(batch_id) REFERENCES provider_receipt_batch(batch_id));
        CREATE TABLE IF NOT EXISTS provider_graph_prepared_node (
        lease_id TEXT NOT NULL,item INTEGER NOT NULL,logical_call INTEGER NOT NULL,node_sha256 TEXT NOT NULL,node_json TEXT NOT NULL,
        PRIMARY KEY(lease_id,item,logical_call),FOREIGN KEY(lease_id) REFERENCES provider_graph_preparation(lease_id));")?;
    Ok(())
}
fn canonical<T: Serialize>(value: &T) -> Result<String> {
    Ok(canonical_json(&serde_json::to_value(value)?))
}
fn lease_identity(lease: &Lease) -> Result<String> {
    let mut value = serde_json::to_value(lease)?;
    value.as_object_mut().unwrap().remove("lease_id");
    Ok(content_hash(&canonical_json(&value)))
}
fn head(conn: &Connection, key: &PlanProfileKey) -> Result<String> {
    let (runtime,sequence,floor,size):(String,i64,i64,usize)=conn.query_row("SELECT runtime_json,source_sequence,observation_floor,window_size FROM provider_graph_execution_state WHERE call_site_version=?1 AND execution_plan_id=?2",
        params![key.call_site_version.as_str(),key.execution_plan_id.as_str()],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?)))?;
    // Ordinary same-cohort publications may advance sequence. Epoch floors,
    // runtime, window and quality/cold boundaries cannot change mid-workflow.
    let quality: Option<QualityHead> = if table_exists(conn, "provider_graph_quality_state")? {
        conn.query_row("SELECT cohort_json,candidate_boundary,disabled_at,reenable_at,evidence_boundary FROM provider_graph_quality_state WHERE call_site_version=?1 AND execution_plan_id=?2",
            params![key.call_site_version.as_str(),key.execution_plan_id.as_str()],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?))).optional()?
    } else {
        None
    };
    if floor <= 0 || sequence < floor || size == 0 {
        bail!("graph preparation execution epoch is invalid");
    }
    Ok(content_hash(&canonical(
        &serde_json::json!({"runtime":runtime,"floor":floor,"window":size,"quality":quality}),
    )?))
}
fn table_exists(conn: &Connection, name: &str) -> Result<bool> {
    Ok(conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name=?1)",
        [name],
        |r| r.get(0),
    )?)
}

fn comparison_context(
    conn: &Connection,
    pair_id: &str,
    template: &Template,
    policy: Policy,
    size: usize,
    now: i64,
) -> Result<ComparisonContext> {
    let pair = crate::provider_graph_comparison::load(conn, pair_id)?;
    if pair.observed_at_us > now {
        bail!("prepared comparison predates its source pair");
    }
    let role = if template.execution_id == pair.reference_execution_id {
        ComparisonRole::Reference
    } else if template.execution_id == pair.candidate_execution_id {
        ComparisonRole::Candidate
    } else {
        bail!("prepared graph template is not a member of its comparison pair");
    };
    crate::provider_graph_quality::window(
        conn,
        crate::provider_graph_quality::Request::from_pair(&pair, size, now, policy),
    )?
    .context("prepared comparison runtime or window has retired")?;
    let generation = crate::provider_graph_quality::current_generation(conn, &pair.key)?;
    let raw: String = conn.query_row(
        "SELECT pair_json FROM provider_graph_pair WHERE pair_id=?1",
        [pair_id],
        |r| r.get(0),
    )?;
    Ok(ComparisonContext {
        source_pair_id: pair_id.into(),
        source_pair_sha256: content_hash(&raw),
        role,
        generation_id: generation.identity()?,
        generation,
        candidate_head_sha256: head(conn, &pair.key)?,
        reference_head_sha256: head(conn, &pair.reference_key)?,
    })
}

fn verify_context_custody(conn: &Connection, lease: &Lease) -> Result<()> {
    let Some(context) = &lease.comparison_context else {
        return Ok(());
    };
    let template = lease
        .template
        .as_ref()
        .context("prepared comparison has no graph template")?;
    let (hash, raw): (String, String) = conn.query_row(
        "SELECT pair_sha256,pair_json FROM provider_graph_pair WHERE pair_id=?1",
        [&context.source_pair_id],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    let pair: crate::provider_graph_comparison::Pair = serde_json::from_str(&raw)?;
    let g = &context.generation;
    let (execution_id, key, runtime) = match context.role {
        ComparisonRole::Reference => (
            &pair.reference_execution_id,
            &pair.reference_key,
            &pair.reference_runtime,
        ),
        ComparisonRole::Candidate => (
            &pair.candidate_execution_id,
            &pair.key,
            &pair.runtime_version,
        ),
    };
    if hash != context.source_pair_sha256
        || content_hash(&raw) != hash
        || pair.pair_id != context.source_pair_id
        || pair.observed_at_us > lease.started_at_us
        || g.identity()? != context.generation_id
        || lease.policy != g.policy
        || lease.window_size != g.window_size
        || pair.key != g.key
        || pair.reference_key != g.reference_key
        || pair.runtime_version != g.runtime
        || pair.reference_runtime != g.reference_runtime
        || template.execution_id != *execution_id
        || template.spec.profile_key()? != *key
        || template.spec.runtime(&template.runtime_branches)? != *runtime
    {
        bail!("prepared comparison custody differs from its original paired generation");
    }
    Ok(())
}

fn verify_context_current(conn: &Connection, lease: &Lease) -> Result<()> {
    if let Some(context) = &lease.comparison_context {
        let g = &context.generation;
        if head(conn, &g.key)? != context.candidate_head_sha256
            || head(conn, &g.reference_key)? != context.reference_head_sha256
            || crate::provider_graph_quality::current_generation(conn, &g.key)? != *g
        {
            bail!("prepared comparison candidate or reference generation has changed");
        }
    }
    Ok(())
}

pub fn begin(
    conn: &mut Connection,
    request: Begin,
    policy: Policy,
    size: usize,
    now: i64,
) -> Result<Lease> {
    let tx = conn.unchecked_transaction()?;
    let lease = begin_inner(&tx, request, policy, size, now, Authorization::Acquisition)?;
    tx.commit()?;
    Ok(lease)
}

pub(crate) fn begin_inner(
    conn: &Connection,
    request: Begin,
    policy: Policy,
    size: usize,
    now: i64,
    authorization: Authorization,
) -> Result<Lease> {
    let (deployment_id, sampling_id) = match authorization {
        Authorization::Acquisition => (None, None),
        Authorization::Deployment(id) => (Some(id), None),
        Authorization::Sampling(id) => (None, Some(id)),
        Authorization::SamplingDeployment {
            sampling,
            deployment,
        } => (Some(deployment), Some(sampling)),
    };
    ensure_schema(conn)?;
    policy.validate()?;
    if now < 0 || size == 0 || request.max_workers == 0 {
        bail!("graph preparation clock, window or workers are invalid");
    }
    if let Some(input) = &request.input_sha256 {
        crate::execution_plan::ExecutionPlanId::parse(input)?;
    }
    let tx = conn;
    let (metadata, sealed): (String, bool) = tx.query_row(
        "SELECT metadata_json,sealed FROM provider_receipt_batch WHERE batch_id=?1",
        [&request.batch_id],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    if deployment_id.is_none() && sampling_id.is_none() {
        let batch: crate::provider_receipts::Batch = serde_json::from_str(&metadata)?;
        crate::provider_graph_campaign::verify_driver(tx, &batch, None)?;
    }
    let batch: crate::provider_receipts::Batch = serde_json::from_str(&metadata)?;
    if request.max_workers > batch.item_count || batch.application_version.is_none() {
        bail!("graph preparation driver is unsupported");
    }
    let old: Option<String> = tx
        .query_row(
            "SELECT lease_id FROM provider_graph_preparation WHERE batch_id=?1",
            [&request.batch_id],
            |r| r.get(0),
        )
        .optional()?;
    if let Some(id) = old {
        let saved = load(tx, &id)?;
        if saved.max_workers != request.max_workers
            || saved.input_sha256 != request.input_sha256
            || saved.policy != policy
            || saved.window_size != size
            || saved.template.as_ref().map(|t| &t.execution_id)
                != request.template_execution_id.as_ref()
            || saved
                .comparison_context
                .as_ref()
                .or(saved.role_comparison_context.as_ref())
                .map(|c| &c.source_pair_id)
                != request.comparison_pair_id.as_ref()
            || saved.role_program_id
                != request
                    .role_program
                    .as_ref()
                    .map(|p| p.identity())
                    .transpose()?
            || saved.deployment_id != deployment_id
            || saved.sampling_id != sampling_id
        {
            bail!("graph preparation retry conflicts");
        }
        return Ok(saved);
    }
    let started: bool = tx.query_row(
        "SELECT EXISTS(SELECT 1 FROM provider_receipt_invocation WHERE batch_id=?1)",
        [&request.batch_id],
        |r| r.get(0),
    )?;
    if sealed || started {
        bail!("graph preparation must precede every SDK dispatch");
    }
    let template=request.template_execution_id.map(|id|->Result<Template> {
        let e=provider_graph::load(tx,&id)?;
        if e.spec.calls.application_version!=batch.application_version.as_deref().unwrap()
            || e.spec.calls.callback_version!=batch.callback_version || e.spec.calls.item_count!=batch.item_count
            || e.spec.calls.max_workers!=request.max_workers || e.source_contracts.is_empty()
            || e.spec.calls.branches.iter().any(|b|b.plan.cache_policy.stable_name!="provider-call") {
            bail!("graph preparation template belongs to another contract or unsupported role graph");
        }
        let (runtime,window):(String,usize)=tx.query_row("SELECT runtime_json,window_size FROM provider_graph_execution_state WHERE call_site_version=?1 AND execution_plan_id=?2",
            params![e.key.call_site_version.as_str(),e.key.execution_plan_id.as_str()],|r|Ok((r.get(0)?,r.get(1)?)))?;
        if runtime!=canonical(&e.runtime)? || window!=size {bail!("graph preparation template runtime or window has retired");}
        Ok(Template {execution_id:id,spec:e.spec,runtime_branches:e.runtime_branches,source_contracts:e.source_contracts,head_sha256:head(tx,&e.key)?})
    }).transpose()?;
    let role_context =
        if let (Some(program), Some(pair)) = (&request.role_program, &request.comparison_pair_id) {
            Some(crate::provider_graph_role_comparison::prepare_context(
                tx, pair, program, policy, size, now,
            )?)
        } else {
            None
        };
    let context = request
        .comparison_pair_id
        .filter(|_| request.role_program.is_none())
        .map(|id| {
            comparison_context(
                tx,
                &id,
                template
                    .as_ref()
                    .context("paired graph preparation requires an explicit template")?,
                policy,
                size,
                now,
            )
        })
        .transpose()?;
    // Establishing the shared quality cohort may update the graph's head.
    let mut template = template;
    if let Some(t) = &mut template {
        t.head_sha256 = head(tx, &t.spec.profile_key()?)?;
    }
    let mut lease = Lease {
        version: VERSION.into(),
        lease_id: String::new(),
        batch_id: request.batch_id,
        metadata_sha256: content_hash(&metadata),
        max_workers: request.max_workers,
        input_sha256: request.input_sha256,
        started_at_us: now,
        policy,
        window_size: size,
        template,
        comparison_context: context,
        acquisition_version: Some(crate::provider_graph_acquisition::VERSION.into()),
        role_comparison_context: role_context,
        role_program_id: request
            .role_program
            .as_ref()
            .map(|p| p.identity())
            .transpose()?,
        deployment_id,
        sampling_id,
    };
    lease.lease_id = lease_identity(&lease)?;
    let raw = canonical(&lease)?;
    tx.execute(
        "INSERT INTO provider_graph_preparation VALUES(?1,?2,?3,?4,NULL)",
        params![lease.lease_id, lease.batch_id, content_hash(&raw), raw],
    )?;
    crate::provider_graph_acquisition::enroll(tx, &lease)?;
    if let Some(program) = &request.role_program {
        crate::provider_graph_roles::enroll(tx, &lease, program)?;
    }
    Ok(lease)
}

/// Common lease custody, before checking the physical program and its ancestry.
pub(crate) fn load_metadata(conn: &Connection, id: &str) -> Result<Lease> {
    let (batch_id, hash, raw): (String, String, String) = conn.query_row(
        "SELECT batch_id,lease_sha256,lease_json FROM provider_graph_preparation WHERE lease_id=?1",
        [id],
        |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
    )?;
    let lease: Lease = serde_json::from_str(&raw)?;
    let metadata: String = conn.query_row(
        "SELECT metadata_json FROM provider_receipt_batch WHERE batch_id=?1",
        [&lease.batch_id],
        |r| r.get(0),
    )?;
    if content_hash(&raw) != hash
        || lease.version != VERSION
        || lease.lease_id != id
        || lease.batch_id != batch_id
        || lease_identity(&lease)? != id
        || content_hash(&metadata) != lease.metadata_sha256
        || lease.started_at_us < 0
        || lease.window_size == 0
        || lease.max_workers == 0
    {
        bail!("graph preparation custody is invalid");
    }
    lease.policy.validate()?;
    verify_context_custody(conn, &lease)?;
    if let Some(version) = &lease.acquisition_version {
        if version!=crate::provider_graph_acquisition::VERSION || !table_exists(conn,"provider_graph_acquisition")?
            || !conn.query_row("SELECT EXISTS(SELECT 1 FROM provider_graph_acquisition WHERE lease_id=?1 AND batch_id=?2)",params![id,lease.batch_id],|r|r.get::<_,bool>(0))? {
            bail!("graph acquisition enrollment is missing or invalid");
        }
    }
    crate::provider_graph_deployment::verify_custody(conn, &lease)?;
    crate::provider_graph_sampling::verify_custody(conn, &lease)?;
    Ok(lease)
}

pub fn load(conn: &Connection, id: &str) -> Result<Lease> {
    let lease = load_metadata(conn, id)?;
    crate::provider_graph_roles::original(conn, &lease)?;
    crate::provider_graph_role_comparison::verify_context_custody(conn, &lease)?;
    Ok(lease)
}

pub fn approve_node(
    conn: &mut Connection,
    id: &str,
    node: &Node,
    policy: Policy,
    size: usize,
    now: i64,
) -> Result<()> {
    ensure_schema(conn)?;
    let tx = conn.transaction()?;
    if load(&tx, id)?.deployment_id.is_some() {
        bail!("guarded primary node approval requires its physical request binding");
    }
    approve_node_inner(&tx, id, node, policy, size, now)?;
    tx.commit()?;
    Ok(())
}

/// Return the frozen template only while the original workflow can still
/// dispatch this logical position. Actual node and physical approval follows.
pub fn template_for_dispatch(
    conn: &Connection,
    id: &str,
    position: crate::multi_call::CallPosition,
    policy: Policy,
    size: usize,
    now: i64,
) -> Result<Template> {
    let lease = load(conn, id)?;
    verify_context_current(conn, &lease)?;
    crate::provider_graph_deployment::verify_current(conn, &lease, now)?;
    let template = lease
        .template
        .as_ref()
        .context("workflow has no primary template")?;
    if lease.policy != policy
        || lease.window_size != size
        || now < lease.started_at_us
        || head(conn, &template.spec.profile_key()?)? != template.head_sha256
    {
        bail!("template planning policy, epoch or clock changed");
    }
    let (used,sealed,began):(bool,bool,bool)=conn.query_row(
        "SELECT execution_id IS NOT NULL,(SELECT sealed FROM provider_receipt_batch WHERE batch_id=p.batch_id),EXISTS(SELECT 1 FROM provider_receipt_invocation WHERE batch_id=p.batch_id AND item=?2 AND logical_call=?3) FROM provider_graph_preparation p WHERE lease_id=?1",
        params![id,position.item,position.call],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?)),
    )?;
    if used || sealed || began {
        bail!("template position has already dispatched or completed");
    }
    if !template
        .spec
        .calls
        .branches
        .iter()
        .any(|b| b.position == position)
    {
        bail!("template has no such logical position");
    }
    verify_dependencies(conn, &lease, position)?;
    Ok(template.clone())
}

pub(crate) fn approve_node_inner(
    conn: &Connection,
    id: &str,
    node: &Node,
    policy: Policy,
    size: usize,
    now: i64,
) -> Result<()> {
    let tx = conn;
    let lease = load(tx, id)?;
    verify_context_current(tx, &lease)?;
    let template = lease
        .template
        .as_ref()
        .context("observation-only graph start has no approved node template")?;
    let key = template.spec.profile_key()?;
    if lease.policy != policy
        || lease.window_size != size
        || now < lease.started_at_us
        || head(tx, &key)? != template.head_sha256
    {
        bail!("prepared graph policy, epoch or clock changed before dispatch");
    }
    let used: Option<String> = tx.query_row(
        "SELECT execution_id FROM provider_graph_preparation WHERE lease_id=?1",
        [id],
        |r| r.get(0),
    )?;
    let sealed: bool = tx.query_row(
        "SELECT sealed FROM provider_receipt_batch WHERE batch_id=?1",
        [&lease.batch_id],
        |r| r.get(0),
    )?;
    if used.is_some() || sealed {
        bail!("prepared graph lease has already completed");
    }
    validate_template_node(template, node)?;
    let raw = canonical(node)?;
    tx.execute(
        "INSERT INTO provider_graph_prepared_node VALUES(?1,?2,?3,?4,?5) ON CONFLICT DO NOTHING",
        params![
            id,
            node.position.item,
            node.position.call,
            content_hash(&raw),
            raw
        ],
    )?;
    let stored:String=tx.query_row("SELECT node_json FROM provider_graph_prepared_node WHERE lease_id=?1 AND item=?2 AND logical_call=?3",params![id,node.position.item,node.position.call],|r|r.get(0))?;
    if stored != raw {
        bail!("prepared graph node is already bound to another request");
    }
    let began:bool=tx.query_row("SELECT EXISTS(SELECT 1 FROM provider_receipt_invocation WHERE batch_id=?1 AND item=?2 AND logical_call=?3)",params![lease.batch_id,node.position.item,node.position.call],|r|r.get(0))?;
    if began {
        bail!("prepared node has already dispatched");
    }
    Ok(())
}

pub(crate) fn validate_template_node(template: &Template, node: &Node) -> Result<()> {
    let branch = template
        .spec
        .calls
        .branches
        .iter()
        .find(|b| b.position == node.position)
        .context("prepared graph has no such logical node")?;
    let runtime = template
        .runtime_branches
        .iter()
        .find(|b| b.position == node.position)
        .context("prepared graph runtime is missing")?;
    let source = template
        .source_contracts
        .iter()
        .find(|s| s.position == node.position)
        .context("prepared graph source controls are missing")?;
    if branch.call_site_version != node.key.call_site_version
        || branch.plan != node.plan
        || branch.plan.execution_plan_id()? != node.key.execution_plan_id
        || runtime.runtime != node.runtime
        || source.sha256 != node.source_contract_sha256
    {
        bail!("SDK node differs from its frozen whole graph");
    }
    crate::execution_plan::ExecutionPlanId::parse(&node.logical_request_sha256)?;
    Ok(())
}

pub(crate) fn verify_dependencies(
    conn: &Connection,
    lease: &Lease,
    position: crate::multi_call::CallPosition,
) -> Result<()> {
    let Some(template) = lease.template.as_ref() else {
        return Ok(());
    };
    for edge in template
        .spec
        .dependencies
        .iter()
        .filter(|e| e.consumer == position)
    {
        let completed:bool=conn.query_row("SELECT EXISTS(SELECT 1 FROM provider_receipt_invocation WHERE batch_id=?1 AND item=?2 AND logical_call=?3 AND ordinal=0 AND json_extract(completion_json,'$.status')='completed')",
            params![lease.batch_id,edge.producer.item,edge.producer.call],|r|r.get(0))?;
        if !completed {
            bail!("prepared graph dependency has not completed");
        }
    }
    Ok(())
}

/// Called by receipt reservation before an approved SDK invocation can start.
pub(crate) fn verify_dispatch(
    conn: &Connection,
    batch: &str,
    call: &crate::provider_receipts::Invocation,
) -> Result<()> {
    if !table_exists(conn, "provider_graph_preparation")? {
        return Ok(());
    }
    let id: Option<String> = conn
        .query_row(
            "SELECT lease_id FROM provider_graph_preparation WHERE batch_id=?1",
            [batch],
            |r| r.get(0),
        )
        .optional()?;
    let Some(id) = id else {
        let metadata: String = conn.query_row(
            "SELECT metadata_json FROM provider_receipt_batch WHERE batch_id=?1",
            [batch],
            |r| r.get(0),
        )?;
        let metadata: crate::provider_receipts::Batch = serde_json::from_str(&metadata)?;
        if metadata.application_version.is_some() {
            crate::provider_graph_campaign::verify_driver(conn, &metadata, None)?;
        }
        return Ok(());
    };
    let lease = load(conn, &id)?;
    verify_context_current(conn, &lease)?;
    crate::provider_graph_deployment::verify_current(
        conn,
        &lease,
        crate::provider_graph_deployment::now_us()?,
    )?;
    crate::provider_graph_deployment::verify_physical(conn, &lease, call)?;
    crate::provider_graph_sampling::verify_dispatch(conn, &lease, call)?;
    crate::provider_graph_campaign::verify_dispatch(conn, &lease, call)?;
    // Exact reservation retries reuse their existing slot. New independent
    // nodes share the declared physical worker limit under the receipt writer
    // transaction, so two concurrent reservations cannot both take its last slot.
    if lease.role_program_id.is_some() || lease.template.is_some() || lease.sampling_id.is_some() {
        let (retry, pending): (bool, u64) = conn.query_row(
            "SELECT EXISTS(SELECT 1 FROM provider_receipt_invocation WHERE batch_id=?1 AND invocation_id=?2),COUNT(*) FILTER(WHERE completion_json IS NULL) FROM provider_receipt_invocation WHERE batch_id=?1",
            params![batch,call.invocation_id], |r| Ok((r.get(0)?,r.get(1)?)),
        )?;
        if !retry && pending >= u64::from(lease.max_workers) {
            bail!("prepared graph physical worker limit is occupied");
        }
    }
    if let Some(program) = crate::provider_graph_roles::original(conn, &lease)? {
        crate::provider_graph_role_comparison::verify_context_current(conn, &lease)?;
        return crate::provider_graph_roles::verify_dispatch(conn, &lease, &program, call);
    }
    let Some(template) = lease.template.as_ref() else {
        return Ok(());
    };
    if head(conn, &template.spec.profile_key()?)? != template.head_sha256 {
        bail!("prepared graph epoch changed before physical dispatch");
    }
    verify_dependencies(conn, &lease, call.position)?;
    verify_prepared_physical(conn, &lease, call)
}

pub(crate) fn verify_prepared_physical(
    conn: &Connection,
    lease: &Lease,
    call: &crate::provider_receipts::Invocation,
) -> Result<()> {
    let id = &lease.lease_id;
    let (hash,raw):(String,String)=conn.query_row("SELECT node_sha256,node_json FROM provider_graph_prepared_node WHERE lease_id=?1 AND item=?2 AND logical_call=?3",params![id,call.position.item,call.position.call],|r|Ok((r.get(0)?,r.get(1)?))).context("prepared graph dispatch has no approved node")?;
    let node: Node = serde_json::from_str(&raw)?;
    let (protocol, model) = if node.plan.provider_protocol == "litellm.completion.v1" {
        (
            "openai.chat.completions.v1",
            node.plan
                .target_model_id
                .strip_prefix("openai/")
                .context("prepared LiteLLM backend is unsupported")?,
        )
    } else {
        (
            node.plan.provider_protocol.as_str(),
            node.plan.target_model_id.as_str(),
        )
    };
    if content_hash(&raw) != hash
        || node.position != call.position
        || call.ordinal != 0
        || call.role != crate::provider_receipts::Role::Primary
        || call.logical_plan_key.as_ref() != Some(&node.key)
        || call.logical_runtime.as_ref() != Some(&node.runtime)
        || call.dispatch_plan_key.as_ref() != Some(&node.key)
        || call.dispatch_runtime.as_ref() != Some(&node.runtime)
        || call.logical_request_sha256.as_deref() != Some(&node.logical_request_sha256)
        || call.provider_protocol != protocol
        || call.requested_model_id != model
        || !call.transport_verified
        || call.request_sha256.is_none()
    {
        bail!("physical SDK dispatch differs from its prepared whole graph");
    }
    Ok(())
}

pub(crate) fn bind_execution(conn: &Connection, e: &Execution) -> Result<()> {
    if table_exists(conn, "provider_graph_preparation")? {
        let prepared: Option<String> = conn
            .query_row(
                "SELECT lease_id FROM provider_graph_preparation WHERE batch_id=?1",
                [&e.batch_id],
                |r| r.get(0),
            )
            .optional()?;
        if prepared.as_deref() != e.preparation_id.as_deref() {
            bail!("graph publication omitted or changed its original preparation");
        }
    }
    let Some(id) = &e.preparation_id else {
        return Ok(());
    };
    let lease = load(conn, id)?;
    verify_context_current(conn, &lease)?;
    crate::provider_graph_sampling::verify_discovery_execution(conn, &lease, e)?;
    if lease.deployment_id.is_some() {
        crate::provider_graph_deployment::verify_current(conn, &lease, e.observed_at_us)?;
        let economics = crate::provider_graph_acquisition::report_batch(conn, &e.batch_id)?
            .context("guarded primary accounting is missing")?;
        if economics.unfinished_batches != 0
            || economics.persistence_failed_batches != 0
            || economics.foreground_latency_ms != e.totals.wall_latency_ms
        {
            bail!("guarded primary publication has unfinished or inconsistent resource accounting");
        }
    }
    if lease.role_program_id.is_some() {
        bail!("prepared role graphs require their complete physical execution contract");
    }
    if lease.batch_id != e.batch_id
        || lease.started_at_us > e.observed_at_us
        || lease.max_workers != e.spec.calls.max_workers
        || e.comparison
            .as_ref()
            .is_some_and(|c| lease.input_sha256.as_ref() != Some(&c.input_sha256))
    {
        bail!("graph execution differs from its original preparation");
    }
    if let Some(template) = &lease.template {
        if template.spec.profile_key()? != e.key
            || template.runtime_branches != e.runtime_branches
            || template.source_contracts != e.source_contracts
            || head(conn, &e.key)? != template.head_sha256
        {
            bail!("prepared graph runtime, controls or epoch changed before publication");
        }
        let count: u64 = conn.query_row(
            "SELECT COUNT(*) FROM provider_graph_prepared_node WHERE lease_id=?1",
            [id],
            |r| r.get(0),
        )?;
        if count != e.spec.calls.branches.len() as u64 {
            bail!("prepared graph node coverage is incomplete");
        }
    }
    let changed=conn.execute("UPDATE provider_graph_preparation SET execution_id=?2 WHERE lease_id=?1 AND (execution_id IS NULL OR execution_id=?2)",params![id,e.execution_id])?;
    if changed != 1 {
        bail!("graph preparation belongs to another execution");
    }
    Ok(())
}

pub fn original_start(conn: &Connection, e: &Execution) -> Result<Option<i64>> {
    let Some(id) = &e.preparation_id else {
        return Ok(None);
    };
    let lease = load(conn, id)?;
    let bound: Option<String> = conn.query_row(
        "SELECT execution_id FROM provider_graph_preparation WHERE lease_id=?1",
        [id],
        |r| r.get(0),
    )?;
    if bound.as_deref() != Some(&e.execution_id)
        || lease.batch_id != e.batch_id
        || lease.started_at_us > e.observed_at_us
    {
        bail!("graph start is not bound to its immutable execution");
    }
    Ok(Some(lease.started_at_us))
}

/// Historical comparisons remain readable; only acquisitions with the shared
/// current epoch may participate in trusted whole-graph readmission.
pub(crate) fn pair_generation_matches(
    conn: &Connection,
    pair: &crate::provider_graph_comparison::Pair,
    generation: &crate::provider_graph_quality::Generation,
) -> Result<bool> {
    let mut contexts = vec![];
    for (id, role, key, runtime) in [
        (
            &pair.reference_execution_id,
            ComparisonRole::Reference,
            &pair.reference_key,
            &pair.reference_runtime,
        ),
        (
            &pair.candidate_execution_id,
            ComparisonRole::Candidate,
            &pair.key,
            &pair.runtime_version,
        ),
    ] {
        let e = provider_graph::load(conn, id)?;
        let Some(preparation) = &e.preparation_id else {
            return Ok(false);
        };
        let lease = load(conn, preparation)?;
        let Some(context) = lease.comparison_context else {
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
        contexts.push(context.generation_id);
    }
    Ok(contexts[0] == contexts[1] && contexts[0] == generation.identity()?)
}
