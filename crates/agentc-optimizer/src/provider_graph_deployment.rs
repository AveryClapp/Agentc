//! Guarded execution of primary templates and fully prepared role graphs. Native
//! workflow admission is immutable; primary requests may bind after producers finish.
//! Completed deployment cost stays visible without being amortized as learning.

use agentc_core::storage::{canonical_json, content_hash};
use anyhow::{bail, Context, Result};
use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};
use serde::{Deserialize, Serialize};

use crate::provider_graph_admission::{self as admission, Decision, Money};
use crate::provider_graph_economics::{self as finance, Kind};
use crate::provider_graph_preparation::{self as preparation, Lease};
use crate::provider_graph_quality::{self as quality, Domain, Generation};
use crate::provider_graph_roles as roles;

pub const VERSION: &str = "provider-graph-deployment-v1";

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Begin {
    pub preparation: preparation::Begin,
    pub admission: admission::Request,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub primary_bindings: Vec<PrimaryBinding>,
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    /// Freeze the graph at admission and bind each request before its SDK call.
    pub dynamic_primary_nodes: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub campaign: Option<crate::provider_graph_campaign::Policy>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PrimaryBinding {
    pub node: preparation::Node,
    pub request_sha256: String,
    pub transport_sha256: String,
}

fn domain(kind: Kind) -> Domain {
    match kind {
        Kind::Primary => Domain::Primary,
        Kind::Roles => Domain::Roles,
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Approval {
    version: String,
    request: Begin,
    decision: Decision,
    generation: Generation,
    horizon_cap: u64,
    ordinal: u64,
}

#[derive(Debug, Serialize)]
pub struct Outcome {
    pub decision: Decision,
    pub lease: Option<Lease>,
}

pub(crate) fn now_us() -> Result<i64> {
    Ok(std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)?
        .as_micros()
        .min(i64::MAX as u128) as i64)
}

fn canonical(value: &impl Serialize) -> Result<String> {
    Ok(canonical_json(&serde_json::to_value(value)?))
}

fn normalized(mut request: Begin) -> Begin {
    // The SDK cannot choose the admission clock. Retries preserve issue time.
    request.admission.policy.selection.now_us = 0;
    request
}

pub fn ensure_schema(conn: &Connection) -> Result<()> {
    preparation::ensure_schema(conn)?;
    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS provider_graph_deployment (
        lease_id TEXT PRIMARY KEY,batch_id TEXT NOT NULL UNIQUE,
        approval_sha256 TEXT NOT NULL,approval_json TEXT NOT NULL,
        FOREIGN KEY(lease_id) REFERENCES provider_graph_preparation(lease_id),
        FOREIGN KEY(batch_id) REFERENCES provider_graph_acquisition(batch_id));
        CREATE TABLE IF NOT EXISTS provider_graph_deployment_node (
        lease_id TEXT NOT NULL,item INTEGER NOT NULL,logical_call INTEGER NOT NULL,
        binding_sha256 TEXT NOT NULL,binding_json TEXT NOT NULL,
        PRIMARY KEY(lease_id,item,logical_call),
        FOREIGN KEY(lease_id,item,logical_call) REFERENCES provider_graph_prepared_node(lease_id,item,logical_call));",
    )?;
    Ok(())
}

fn load(conn: &Connection, lease: &Lease) -> Result<Option<Approval>> {
    let Some(id) = &lease.deployment_id else {
        return Ok(None);
    };
    let (batch, hash, raw): (String, String, String) = conn.query_row(
        "SELECT batch_id,approval_sha256,approval_json FROM provider_graph_deployment WHERE lease_id=?1",
        [&lease.lease_id], |r| Ok((r.get(0)?,r.get(1)?,r.get(2)?)),
    ).context("guarded graph deployment custody is missing")?;
    let approval: Approval = serde_json::from_str(&raw)?;
    let r = &approval.request;
    let decision = &approval.decision;
    let mut issued_request = decision.request.clone();
    issued_request.remaining_batches = r.admission.remaining_batches;
    if hash != *id
        || content_hash(&raw) != hash
        || canonical(&approval)? != raw
        || approval.version != VERSION
        || batch != lease.batch_id
        || r.preparation.batch_id != batch
        || r.preparation.max_workers != lease.max_workers
        || r.preparation.input_sha256 != lease.input_sha256
        || r.preparation.template_execution_id.as_ref()
            != lease.template.as_ref().map(|t| &t.execution_id)
        || r.preparation.comparison_pair_id.is_some()
        || r.preparation
            .role_program
            .as_ref()
            .map(|p| p.identity())
            .transpose()?
            != lease.role_program_id
        || match r.admission.kind {
            Kind::Roles => {
                lease.role_program_id.is_none()
                    || lease.template.is_some()
                    || !r.primary_bindings.is_empty()
                    || r.dynamic_primary_nodes
            }
            Kind::Primary => {
                lease.role_program_id.is_some()
                    || lease.template.is_none()
                    || if r.dynamic_primary_nodes {
                        !r.primary_bindings.is_empty()
                    } else {
                        r.primary_bindings.is_empty()
                    }
            }
        }
        || r.admission.policy.quality != lease.policy
        || r.admission.window_size != lease.window_size
        || r.admission.policy.selection.now_us != 0
        || decision.selected_reference
        || decision.selected_source_pair_id.is_none()
        || decision.evidence_sha256 != content_hash(&decision.evidence_json()?)
        || decision.request.policy.selection.now_us != lease.started_at_us
        || normalized(Begin {
            preparation: r.preparation.clone(),
            admission: issued_request,
            primary_bindings: r.primary_bindings.clone(),
            dynamic_primary_nodes: r.dynamic_primary_nodes,
            campaign: r.campaign.clone(),
        }) != *r
        || approval.generation.key != decision.selected_key
        || approval.generation.policy != lease.policy
        || approval.generation.window_size != lease.window_size
        || approval.ordinal == 0
        || approval.ordinal > approval.horizon_cap
        || decision.request.remaining_batches > r.admission.remaining_batches
        || decision.request.remaining_batches > approval.horizon_cap - approval.ordinal + 1
    {
        bail!("guarded graph deployment custody differs from its original approval");
    }
    Ok(Some(approval))
}

fn horizon(conn: &Connection, batch: &str) -> Result<Option<(u64, u64)>> {
    let metadata: String = conn.query_row(
        "SELECT metadata_json FROM provider_receipt_batch WHERE batch_id=?1",
        [batch],
        |r| r.get(0),
    )?;
    let driver =
        crate::provider_graph_acquisition::Driver::from_batch(&serde_json::from_str(&metadata)?)?
            .identity()?;
    let (count,first):(u64,Option<i64>)=conn.query_row("SELECT COUNT(*),MIN(a.rowid) FROM provider_graph_acquisition a JOIN provider_graph_preparation p USING(lease_id) WHERE a.driver_id=?1 AND json_extract(p.lease_json,'$.deployment_id') IS NOT NULL",[&driver],|r|Ok((r.get(0)?,r.get(1)?)))?;
    if count == 0 {
        return Ok(None);
    }
    let covered:u64=conn.query_row("SELECT COUNT(*) FROM provider_graph_deployment d JOIN provider_graph_acquisition a USING(batch_id) WHERE a.driver_id=?1",[&driver],|r|r.get(0))?;
    if covered != count {
        bail!("deployment horizon custody is missing");
    }
    let id: String = conn.query_row(
        "SELECT lease_id FROM provider_graph_acquisition WHERE rowid=?1",
        [first.unwrap()],
        |r| r.get(0),
    )?;
    let lease = preparation::load(conn, &id)?;
    let approval = load(conn, &lease)?.context("original deployment horizon is missing")?;
    Ok(Some((approval.horizon_cap, count)))
}

pub(crate) fn verify_custody(conn: &Connection, lease: &Lease) -> Result<()> {
    if let Some(approval) = load(conn, lease)? {
        verify_bound_nodes(conn, lease, &approval)?;
    }
    Ok(())
}

pub fn validate_sdk_policy(
    request: &Begin,
    quality: quality::Policy,
    active: &crate::execution_plan::SelectionPolicy,
    size: usize,
) -> Result<()> {
    validate_sdk_admission(
        &request.admission.policy,
        request.admission.window_size,
        quality,
        active,
        size,
    )
}

pub fn validate_sdk_admission(
    policy: &admission::Policy,
    window: usize,
    quality: quality::Policy,
    active: &crate::execution_plan::SelectionPolicy,
    size: usize,
) -> Result<()> {
    if policy.quality != quality
        || window != size
        || policy.selection.objective != active.objective
        || policy.selection.min_paired_observations < active.min_paired_observations
        || policy.selection.max_profile_age_us > active.max_profile_age_us
        || policy.selection.divergence_exposure_budget != active.divergence_exposure_budget
    {
        bail!("guarded graph admission policy differs from active SDK configuration");
    }
    Ok(())
}

pub fn verify_dispatch_policy(
    conn: &Connection,
    batch: &str,
    quality: quality::Policy,
    active: &crate::execution_plan::SelectionPolicy,
    size: usize,
) -> Result<()> {
    let exists:bool=conn.query_row("SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='provider_graph_preparation')",[],|r|r.get(0))?;
    if !exists {
        return Ok(());
    }
    let id: Option<String> = conn
        .query_row(
            "SELECT lease_id FROM provider_graph_preparation WHERE batch_id=?1",
            [batch],
            |r| r.get(0),
        )
        .optional()?;
    if let Some(id) = id {
        let lease = preparation::load(conn, &id)?;
        if let Some(approval) = load(conn, &lease)? {
            validate_sdk_policy(&approval.request, quality, active, size)?;
        }
    }
    Ok(())
}

fn validate_program(conn: &Connection, request: &Begin, decision: &Decision) -> Result<()> {
    if request.admission.kind == Kind::Primary {
        return validate_primary(conn, request, decision);
    }
    let upcoming = request
        .preparation
        .role_program
        .as_ref()
        .context("guarded role deployment needs all physical request and transport bindings")?;
    upcoming.validate()?;
    if request.admission.kind != Kind::Roles
        || request.preparation.template_execution_id.is_some()
        || request.preparation.comparison_pair_id.is_some()
        || request.preparation.input_sha256.is_none()
        || !request.primary_bindings.is_empty()
        || request.dynamic_primary_nodes
    {
        bail!(
            "guarded deployment requires a complete role program and a workflow input fingerprint"
        );
    }
    let source_id = decision
        .selected_source_pair_id
        .as_ref()
        .context("no candidate graph is admitted")?;
    let pair = crate::provider_graph_role_comparison::load(conn, source_id)?;
    let e = roles::load(conn, &pair.candidate_execution_id)?;
    let original_lease = preparation::load(conn, &e.lease_id)?;
    let original =
        roles::original(conn, &original_lease)?.context("original candidate program is missing")?;
    let mut controls = upcoming.source_contracts.clone();
    let mut original_controls = original.source_contracts.clone();
    controls.sort_by_key(|c| c.position);
    original_controls.sort_by_key(|c| c.position);
    if upcoming.program != original.program
        || upcoming.runtime()? != original.runtime()?
        || controls != original_controls
        || upcoming.program.key()? != decision.selected_key
        || request.preparation.max_workers != upcoming.program.graph.calls.max_workers
    {
        bail!("upcoming deployment differs from the admitted original graph, runtime or source controls");
    }
    Ok(())
}

fn validate_primary(conn: &Connection, request: &Begin, decision: &Decision) -> Result<()> {
    let source_id = decision
        .selected_source_pair_id
        .as_ref()
        .context("no primary graph is admitted")?;
    let pair = crate::provider_graph_comparison::load(conn, source_id)?;
    let e = crate::provider_graph::load(conn, &pair.candidate_execution_id)?;
    if request.preparation.template_execution_id.as_deref() != Some(e.execution_id.as_str())
        || request.preparation.role_program.is_some()
        || request.preparation.comparison_pair_id.is_some()
        || request.preparation.input_sha256.is_none()
        || request.preparation.max_workers != e.spec.calls.max_workers
        || if request.dynamic_primary_nodes {
            !request.primary_bindings.is_empty()
        } else {
            request.primary_bindings.len() != e.spec.calls.branches.len()
        }
        || e.key != decision.selected_key
    {
        bail!("guarded primary deployment requires its admitted template and complete physical bindings");
    }
    let mut seen = std::collections::BTreeSet::new();
    for binding in &request.primary_bindings {
        let node = &binding.node;
        let branch = e
            .spec
            .calls
            .branches
            .iter()
            .find(|b| b.position == node.position)
            .context("primary binding names an unknown node")?;
        let runtime = e
            .runtime_branches
            .iter()
            .find(|b| b.position == node.position)
            .context("primary binding runtime is missing")?;
        let control = e
            .source_contracts
            .iter()
            .find(|b| b.position == node.position)
            .context("primary source controls are missing")?;
        if !seen.insert(node.position)
            || node.plan != branch.plan
            || node.key.call_site_version != branch.call_site_version
            || node.key.execution_plan_id != branch.plan.execution_plan_id()?
            || node.runtime != runtime.runtime
            || node.source_contract_sha256 != control.sha256
        {
            bail!("primary deployment binding differs from its admitted original node");
        }
        for hash in [
            &node.logical_request_sha256,
            &binding.request_sha256,
            &binding.transport_sha256,
        ] {
            crate::execution_plan::ExecutionPlanId::parse(hash)?;
        }
    }
    Ok(())
}

pub(crate) fn verify_physical(
    conn: &Connection,
    lease: &Lease,
    call: &crate::provider_receipts::Invocation,
) -> Result<()> {
    let Some(approval) = load(conn, lease)? else {
        return Ok(());
    };
    if approval.request.admission.kind != Kind::Primary {
        return Ok(());
    }
    verify_primary_binding(conn, lease, &approval, call)?;
    let sealed: bool = conn.query_row(
        "SELECT sealed FROM provider_receipt_batch WHERE batch_id=?1",
        [&lease.batch_id],
        |r| r.get(0),
    )?;
    if sealed {
        bail!("guarded primary foreground has already sealed");
    }
    Ok(())
}

fn verify_primary_binding(
    conn: &Connection,
    lease: &Lease,
    approval: &Approval,
    call: &crate::provider_receipts::Invocation,
) -> Result<()> {
    let binding = primary_binding(conn, lease, approval, call.position)?;
    let node = &binding.node;
    if call.ordinal != 0
        || call.role != crate::provider_receipts::Role::Primary
        || call.request_sha256.as_ref() != Some(&binding.request_sha256)
        || call.transport_sha256.as_ref() != Some(&binding.transport_sha256)
        || call.logical_request_sha256.as_ref() != Some(&node.logical_request_sha256)
        || call.logical_plan_key.as_ref() != Some(&node.key)
        || call.dispatch_plan_key.as_ref() != Some(&node.key)
        || call.logical_runtime.as_ref() != Some(&node.runtime)
        || call.dispatch_runtime.as_ref() != Some(&node.runtime)
        || call.parent_plan_key.is_some()
    {
        bail!("primary SDK request or transport differs from its guarded approval");
    }
    Ok(())
}

fn primary_binding(
    conn: &Connection,
    lease: &Lease,
    approval: &Approval,
    position: crate::multi_call::CallPosition,
) -> Result<PrimaryBinding> {
    if !approval.request.dynamic_primary_nodes {
        return approval
            .request
            .primary_bindings
            .iter()
            .find(|b| b.node.position == position)
            .cloned()
            .context("guarded primary dispatch has no physical binding");
    }
    let (hash,raw):(String,String)=conn.query_row(
        "SELECT binding_sha256,binding_json FROM provider_graph_deployment_node WHERE lease_id=?1 AND item=?2 AND logical_call=?3",
        params![lease.lease_id,position.item,position.call],|r|Ok((r.get(0)?,r.get(1)?)),
    ).context("dynamic primary request has not been prepared")?;
    let binding: PrimaryBinding = serde_json::from_str(&raw)?;
    if binding.node.position != position
        || content_hash(&raw) != hash
        || canonical(&binding)? != raw
    {
        bail!("dynamic primary binding custody is invalid");
    }
    preparation::validate_template_node(
        lease
            .template
            .as_ref()
            .context("primary template is missing")?,
        &binding.node,
    )?;
    for fingerprint in [&binding.request_sha256, &binding.transport_sha256] {
        crate::execution_plan::ExecutionPlanId::parse(fingerprint)?;
    }
    let (node_hash,node_raw):(String,String)=conn.query_row(
        "SELECT node_sha256,node_json FROM provider_graph_prepared_node WHERE lease_id=?1 AND item=?2 AND logical_call=?3",
        params![lease.lease_id,position.item,position.call],|r|Ok((r.get(0)?,r.get(1)?)),
    )?;
    if content_hash(&node_raw) != node_hash || canonical(&binding.node)? != node_raw {
        bail!("dynamic primary binding differs from its approved node");
    }
    Ok(binding)
}

fn verify_bound_nodes(conn: &Connection, lease: &Lease, approval: &Approval) -> Result<()> {
    if !approval.request.dynamic_primary_nodes {
        return Ok(());
    }
    let positions = {
        let mut q = conn.prepare(
            "SELECT item,logical_call FROM provider_graph_deployment_node WHERE lease_id=?1",
        )?;
        let rows = q
            .query_map([&lease.lease_id], |r| {
                Ok(crate::multi_call::CallPosition {
                    item: r.get(0)?,
                    call: r.get(1)?,
                })
            })?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        rows
    };
    for position in &positions {
        primary_binding(conn, lease, approval, *position)?;
    }
    let prepared: u64 = conn.query_row(
        "SELECT COUNT(*) FROM provider_graph_prepared_node WHERE lease_id=?1",
        [&lease.lease_id],
        |r| r.get(0),
    )?;
    let missing:bool=conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM provider_receipt_invocation i LEFT JOIN provider_graph_deployment_node n ON n.lease_id=?1 AND n.item=i.item AND n.logical_call=i.logical_call WHERE i.batch_id=?2 AND n.lease_id IS NULL)",
        params![lease.lease_id,lease.batch_id],|r|r.get(0),
    )?;
    if prepared != positions.len() as u64 || missing {
        bail!("dynamic primary binding coverage is incomplete");
    }
    Ok(())
}

/// Bind the actual request only when its producers have completed. Node and
/// physical fingerprints are committed together, before physical reservation.
pub fn approve_primary_node(
    conn: &mut Connection,
    lease_id: &str,
    binding: &PrimaryBinding,
    policy: quality::Policy,
    size: usize,
    now: i64,
) -> Result<()> {
    ensure_schema(conn)?;
    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let lease = preparation::load(&tx, lease_id)?;
    let approval =
        load(&tx, &lease)?.context("request binding requires guarded primary deployment")?;
    if approval.request.admission.kind != Kind::Primary {
        bail!("request binding uses another graph domain");
    }
    verify_current(&tx, &lease, now)?;
    for fingerprint in [&binding.request_sha256, &binding.transport_sha256] {
        crate::execution_plan::ExecutionPlanId::parse(fingerprint)?;
    }
    if !approval.request.dynamic_primary_nodes
        && primary_binding(&tx, &lease, &approval, binding.node.position)? != *binding
    {
        bail!("primary request differs from its original frozen approval");
    }
    preparation::verify_dependencies(&tx, &lease, binding.node.position)?;
    preparation::approve_node_inner(&tx, lease_id, &binding.node, policy, size, now)?;
    if approval.request.dynamic_primary_nodes {
        let raw = canonical(binding)?;
        tx.execute("INSERT INTO provider_graph_deployment_node VALUES(?1,?2,?3,?4,?5) ON CONFLICT DO NOTHING",
            params![lease_id,binding.node.position.item,binding.node.position.call,content_hash(&raw),raw])?;
        if primary_binding(&tx, &lease, &approval, binding.node.position)? != *binding {
            bail!("dynamic primary request is already bound to another dispatch");
        }
        verify_bound_nodes(&tx, &lease, &approval)?;
    }
    tx.commit()?;
    Ok(())
}

/// Publish the original callback result and SDK receipts without replaying any
/// application work or promoting a whole result into per-node quality evidence.
pub fn finish_primary(
    conn: &mut Connection,
    profiles: &crate::plan_profile::PlanProfiles,
    lease_id: &str,
    comparison: crate::provider_graph::Comparison,
    now: i64,
) -> Result<crate::provider_graph::Execution> {
    use crate::multi_call::MultiCallOutcome;
    use crate::provider_receipts::{Completion, Invocation, Status};
    let (lease, template, requests, outcomes, wall) = {
        let tx = conn.unchecked_transaction()?;
        let lease = preparation::load(&tx, lease_id)?;
        let approval =
            load(&tx, &lease)?.context("primary completion requires guarded deployment custody")?;
        if approval.request.admission.kind != Kind::Primary
            || profiles.retained_window_size() != lease.window_size
        {
            bail!("guarded primary completion uses another graph domain or window");
        }
        comparison.validate()?;
        if lease.input_sha256.as_ref() != Some(&comparison.input_sha256)
            || now < lease.started_at_us
        {
            bail!("guarded primary callback result differs from its workflow input or clock");
        }
        let template = lease
            .template
            .clone()
            .context("guarded primary template is missing")?;
        let resources = crate::provider_graph_acquisition::report_batch(&tx, &lease.batch_id)?
            .context("primary resource accounting is missing")?;
        if resources.unfinished_batches != 0 || resources.persistence_failed_batches != 0 {
            bail!("primary resource accounting is unfinished or failed");
        }
        let rows = {
            let mut q=tx.prepare("SELECT invocation_json,completion_json FROM provider_receipt_invocation WHERE batch_id=?1 ORDER BY item,logical_call,ordinal")?;
            let rows = q
                .query_map([&lease.batch_id], |r| {
                    Ok((r.get::<_, String>(0)?, r.get::<_, Option<String>>(1)?))
                })?
                .collect::<rusqlite::Result<Vec<_>>>()?;
            rows
        };
        let mut requests = std::collections::BTreeMap::new();
        let mut outcomes = vec![];
        for (call, done) in rows {
            let call: Invocation = serde_json::from_str(&call)?;
            let done: Completion =
                serde_json::from_str(&done.context("primary dispatch has no terminal receipt")?)?;
            verify_primary_binding(&tx, &lease, &approval, &call)?;
            if done.status != Status::Completed || done.http_attempts != Some(1) {
                bail!("guarded primary graph includes failed or retried physical work");
            }
            requests.insert(
                call.position,
                call.logical_request_sha256
                    .clone()
                    .context("primary logical request is missing")?,
            );
            outcomes.push(MultiCallOutcome {
                position: call.position,
                cost_source: done.cost_source.clone(),
                outcome: crate::Outcome {
                    input_tokens: done
                        .input_tokens
                        .context("primary input usage is missing")?,
                    output_tokens: done
                        .output_tokens
                        .context("primary output usage is missing")?,
                    latency_ms: done.latency_ms,
                    cost_usd: done.cost_usd.unwrap_or(0.),
                    cost_source: done.cost_source,
                    inference_cost_complete: Some(resources.cost_complete),
                    physical_invocations: vec![crate::cost_evidence::PhysicalInvocation {
                        batch_id: lease.batch_id.clone(),
                        invocation_id: call.invocation_id,
                    }],
                    ..Default::default()
                },
            });
        }
        let wall = resources.foreground_latency_ms;
        tx.commit()?;
        (lease, template, requests, outcomes, wall)
    };
    let input = lease
        .input_sha256
        .as_ref()
        .context("guarded workflow input is missing")?;
    crate::provider_graph::publish(
        conn,
        profiles,
        crate::provider_graph::PublishRequest {
            batch_id: &lease.batch_id,
            spec: &template.spec,
            versions: &template.runtime_branches,
            outcomes: &outcomes,
            requests: &requests,
            wall_latency_ms: wall,
            input_sha256: input,
            observed_at_us: now,
            comparison: Some(comparison),
            source_contracts: &template.source_contracts,
            preparation_id: Some(lease_id),
        },
    )
}

/// Native callers provide the clock; the SDK command always uses native time.
/// A held decision commits its quality projection and creates no preparation.
pub fn begin(conn: &mut Connection, request: Begin, now: i64) -> Result<Outcome> {
    ensure_schema(conn)?;
    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let outcome = begin_inner(&tx, request, now, None, 0)?;
    tx.commit()?;
    Ok(outcome)
}

/// Sampling and guarded admission commit under the same original writer lease.
pub(crate) fn begin_inner(
    tx: &Connection,
    request: Begin,
    now: i64,
    sampling: Option<&str>,
    future_samples: u64,
) -> Result<Outcome> {
    if now < 0 {
        bail!("guarded graph clock is invalid");
    }
    if let Some(policy) = &request.campaign {
        policy.validate()?;
        if request.admission.policy.max_physical_invocations > policy.max_physical_invocations {
            bail!(
                "deployment campaign must own one independent workflow within its call allowance"
            );
        }
    }
    let request = normalized(request);
    let metadata: String = tx.query_row(
        "SELECT metadata_json FROM provider_receipt_batch WHERE batch_id=?1",
        [&request.preparation.batch_id],
        |r| r.get(0),
    )?;
    let batch = serde_json::from_str(&metadata)?;
    crate::provider_graph_campaign::verify_driver(tx, &batch, request.campaign.as_ref())?;
    let old: Option<String> = tx
        .query_row(
            "SELECT lease_id FROM provider_graph_preparation WHERE batch_id=?1",
            [&request.preparation.batch_id],
            |r| r.get(0),
        )
        .optional()?;
    if let Some(id) = old {
        let lease = preparation::load(tx, &id)?;
        let approval =
            load(tx, &lease)?.context("acquisition preparation cannot become a deployment")?;
        if approval.request != request || lease.sampling_id.as_deref() != sampling {
            bail!("guarded graph preparation retry changed its request");
        }
        verify_current(tx, &lease, now)?;
        return Ok(Outcome {
            decision: approval.decision,
            lease: Some(lease),
        });
    }
    let (mut decision, cap, issued) =
        select_for_campaign_begin(tx, &request, now, future_samples, sampling)?;
    let admission_request = decision.request.clone();
    if decision.selected_reference {
        return Ok(Outcome {
            decision,
            lease: None,
        });
    }
    validate_program(tx, &request, &decision)?;
    let selected = decision
        .alternatives
        .iter()
        .find(|a| a.key == decision.selected_key)
        .and_then(|a| a.estimate.as_ref())
        .context("admitted graph estimate is missing")?;
    if selected.readmission_required {
        quality::finish_readmission_after_admission_for(
            domain(request.admission.kind),
            tx,
            &selected.quality,
            now,
        )?;
        decision = admission::select_with_future_samples(tx, admission_request, future_samples)?;
        if decision.selected_reference {
            bail!("graph no longer qualifies after cold readmission");
        }
        validate_program(tx, &request, &decision)?;
    }
    let generation = quality::current_generation_for(
        domain(request.admission.kind),
        tx,
        &decision.selected_key,
    )?;
    let approval = Approval {
        version: VERSION.into(),
        request: request.clone(),
        decision: decision.clone(),
        generation,
        horizon_cap: cap,
        ordinal: issued + 1,
    };
    let raw = canonical(&approval)?;
    let id = content_hash(&raw);
    let lease = preparation::begin_inner(
        tx,
        request.preparation.clone(),
        decision.request.policy.quality,
        decision.request.window_size,
        now,
        match sampling {
            Some(sampling) => preparation::Authorization::SamplingDeployment {
                sampling: sampling.into(),
                deployment: id.clone(),
            },
            None => preparation::Authorization::Deployment(id.clone()),
        },
    )?;
    tx.execute(
        "INSERT INTO provider_graph_deployment VALUES(?1,?2,?3,?4)",
        params![lease.lease_id, lease.batch_id, id, raw],
    )?;
    if let Some(policy) = request.campaign.as_ref().filter(|_| sampling.is_none()) {
        crate::provider_graph_campaign::enroll_deployment(tx, policy, &lease, &id)?;
    }
    for binding in &request.primary_bindings {
        preparation::approve_node_inner(
            tx,
            &lease.lease_id,
            &binding.node,
            lease.policy,
            lease.window_size,
            now,
        )?;
    }
    verify_custody(tx, &lease)?;
    Ok(Outcome {
        decision,
        lease: Some(lease),
    })
}

pub(crate) fn select_for_begin(
    conn: &Connection,
    request: &Begin,
    now: i64,
    future_samples: u64,
) -> Result<(Decision, u64, u64)> {
    select_for_campaign_begin(conn, request, now, future_samples, None)
}

fn select_for_campaign_begin(
    conn: &Connection,
    request: &Begin,
    now: i64,
    future_samples: u64,
    issuing_ticket: Option<&str>,
) -> Result<(Decision, u64, u64)> {
    let mut admission = request.admission.clone();
    admission.policy.selection.now_us = now;
    let (cap, issued) =
        horizon(conn, &request.preparation.batch_id)?.unwrap_or((admission.remaining_batches, 0));
    let remaining = cap
        .checked_sub(issued)
        .filter(|n| *n > 0)
        .context("deployment workload horizon is exhausted")?;
    admission.remaining_batches = admission.remaining_batches.min(remaining);
    if let Some(campaign) = &request.campaign {
        let funded = crate::provider_graph_campaign::remaining_horizon(
            conn,
            campaign,
            issuing_ticket,
            false,
        )?;
        if funded == 0 {
            bail!("shared campaign cannot fund another deployment forecast");
        }
        admission.remaining_batches = admission.remaining_batches.min(funded);
    }
    Ok((
        admission::select_with_future_samples(conn, admission, future_samples)?,
        cap,
        issued,
    ))
}

pub(crate) fn decision(conn: &Connection, lease: &Lease) -> Result<Decision> {
    Ok(load(conn, lease)?
        .context("original guarded decision is missing")?
        .decision)
}

/// Runs inside the receipt writer transaction before any physical reservation.
pub(crate) fn verify_current(conn: &Connection, lease: &Lease, now: i64) -> Result<()> {
    let Some(approval) = load(conn, lease)? else {
        return Ok(());
    };
    verify_bound_nodes(conn, lease, &approval)?;
    if now < lease.started_at_us
        || quality::current_generation_for(
            domain(approval.request.admission.kind),
            conn,
            &approval.decision.selected_key,
        )? != approval.generation
    {
        bail!("guarded graph quality generation has retired");
    }
    let mut request = approval.decision.request.clone();
    request.policy.selection.now_us = now;
    let (cap, issued) =
        horizon(conn, &lease.batch_id)?.context("deployment workload horizon is missing")?;
    request.remaining_batches = request.remaining_batches.min(
        cap.checked_sub(issued)
            .context("deployment horizon is invalid")?
            .saturating_add(1),
    );
    if let Some(remaining) = crate::provider_graph_sampling::remaining_horizon(conn, lease)? {
        request.remaining_batches = request.remaining_batches.min(remaining);
    }
    if let Some(campaign) = &approval.request.campaign {
        request.remaining_batches =
            request
                .remaining_batches
                .min(crate::provider_graph_campaign::remaining_horizon(
                    conn, campaign, None, true,
                )?);
    }
    let current = admission::select_for_dispatch(
        conn,
        request,
        &lease.lease_id,
        approval.decision.future_samples,
    )?;
    if current.selected_reference
        || current.selected_key != approval.decision.selected_key
        || current.selected_source_pair_id != approval.decision.selected_source_pair_id
        || current
            .alternatives
            .iter()
            .find(|a| a.key == current.selected_key)
            .and_then(|a| a.estimate.as_ref())
            .is_none_or(|e| e.readmission_required)
    {
        bail!("guarded graph is no longer admitted before physical dispatch");
    }
    Ok(())
}

/// Partition only custody-verified normal work. Partial/failed/unpublished or
/// unpriced deployment remains an acquisition liability. The one active lease
/// being rechecked retains its explicitly frozen work in deployment accounting.
pub(crate) fn partition(
    conn: &Connection,
    batches: Vec<String>,
    current_lease: Option<&str>,
) -> Result<(Vec<String>, Vec<String>)> {
    let mut acquisition = vec![];
    let mut deployed = vec![];
    for batch in batches {
        let (id, guarded): (String,bool) = conn.query_row(
            "SELECT a.lease_id,json_extract(p.lease_json,'$.deployment_id') IS NOT NULL FROM provider_graph_acquisition a JOIN provider_graph_preparation p USING(lease_id) WHERE a.batch_id=?1",
            [&batch],
            |r| Ok((r.get(0)?,r.get(1)?)),
        )?;
        if !guarded {
            acquisition.push(batch);
            continue;
        }
        let lease = preparation::load(conn, &id)?;
        let Some(approval) = load(conn, &lease)? else {
            acquisition.push(batch);
            continue;
        };
        if current_lease == Some(id.as_str()) {
            deployed.push(batch);
            continue;
        }
        let (table, column) = match approval.request.admission.kind {
            Kind::Primary => ("provider_graph_preparation", "execution_id"),
            Kind::Roles => ("provider_graph_role_execution", "execution_id"),
        };
        let execution: Option<String> = conn
            .query_row(
                &format!("SELECT {column} FROM {table} WHERE lease_id=?1"),
                [&id],
                |r| r.get::<_, Option<String>>(0),
            )
            .optional()?
            .flatten();
        let complete = if let Some(execution) = execution {
            let projection = finance::execution(conn, approval.request.admission.kind, &execution)?;
            projection.key == approval.decision.selected_key
                && projection.resource_latency_ms.is_some()
                && match &approval.request.admission.policy.money {
                    Money::BilledUsd => projection.costs.billed_usd.is_some(),
                    Money::PublishedTokenTariffUsd {
                        snapshot_sha256, ..
                    } => projection
                        .costs
                        .published_token_tariff
                        .as_ref()
                        .is_some_and(|b| &b.snapshot_sha256 == snapshot_sha256),
                }
        } else {
            false
        };
        if complete {
            deployed.push(batch);
        } else {
            acquisition.push(batch);
        }
    }
    Ok((acquisition, deployed))
}
