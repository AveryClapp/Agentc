//! Prepared physical role graphs. Dispatches and result selection are frozen
//! before SDK work; successful execution supplies custody, never admission.

use std::collections::{BTreeMap, BTreeSet};

use agentc_core::storage::{canonical_json, content_hash};
use anyhow::{bail, Context, Result};
use rusqlite::{params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};

use crate::execution_plan::ExecutionPlanSpec;
use crate::multi_call::CallPosition;
use crate::plan_profile::{CallSiteVersion, PlanProfileKey, PlanRuntimeVersion};
use crate::provider_graph::{SourceContract, Spec};
use crate::provider_graph_preparation::Lease;
use crate::provider_receipts::{Completion, Invocation, Role, Status};

pub const VERSION: &str = "provider-role-graph-v1";
pub const RUNTIME_VERSION: &str = "provider-role-graph-runtime-v1";

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Slot {
    pub position: CallPosition,
    pub ordinal: u32,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum Activation {
    Always,
    /// Cancellation and successful responses do not authorize a fallback.
    OnFailure {
        predecessor: Slot,
    },
    Validate {
        subject: Slot,
        contract_sha256: String,
    },
    OnValidationRejection {
        predecessor: Slot,
        validator: Slot,
    },
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Dispatch {
    pub slot: Slot,
    pub role: Role,
    pub call_site_version: CallSiteVersion,
    pub plan: ExecutionPlanSpec,
    pub activation: Activation,
}

impl Dispatch {
    pub fn key(&self) -> Result<PlanProfileKey> {
        Ok(PlanProfileKey {
            call_site_version: self.call_site_version.clone(),
            execution_plan_id: self.plan.execution_plan_id()?,
        })
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Program {
    pub version: String,
    pub graph: Spec,
    /// Per-node ordinals are a declared serial physical schedule. Distinct
    /// logical nodes may overlap according to the graph's dependencies.
    pub dispatches: Vec<Dispatch>,
}

impl Program {
    pub fn validate(&self) -> Result<()> {
        self.graph.validate()?;
        if self.version != VERSION || self.dispatches.is_empty() {
            bail!("physical graph version or schedule is unsupported");
        }
        let nodes: BTreeMap<_, _> = self
            .graph
            .calls
            .branches
            .iter()
            .map(|b| (b.position, b))
            .collect();
        let mut slots = BTreeMap::new();
        let mut ordered: Vec<_> = self.dispatches.iter().collect();
        ordered.sort_by_key(|d| d.slot);
        let mut next = BTreeMap::<CallPosition, u32>::new();
        for dispatch in ordered {
            let node = nodes
                .get(&dispatch.slot.position)
                .context("physical graph has an unknown logical node")?;
            let ordinal = next.entry(dispatch.slot.position).or_default();
            if dispatch.slot.ordinal != *ordinal || slots.insert(dispatch.slot, dispatch).is_some()
            {
                bail!("physical graph ordinals are duplicated or have gaps");
            }
            *ordinal = ordinal
                .checked_add(1)
                .context("physical graph ordinal overflow")?;
            dispatch.key()?;
            if dispatch.plan.cache_policy.stable_name != "provider-call" {
                bail!("physical graph requires explicit provider calls; cache provenance is unsupported");
            }
            if dispatch.slot.ordinal == 0 {
                if dispatch.role != Role::Primary
                    || dispatch.activation != Activation::Always
                    || dispatch.call_site_version != node.call_site_version
                    || dispatch.plan != node.plan
                {
                    bail!("physical graph primary differs from its logical node");
                }
            } else {
                if dispatch.role == Role::Primary {
                    bail!("physical graph has an extra primary");
                }
                match &dispatch.activation {
                    Activation::Always if dispatch.role != Role::Fallback => {}
                    Activation::Validate {
                        subject,
                        contract_sha256,
                    } if dispatch.role == Role::Validation => {
                        crate::execution_plan::ExecutionPlanId::parse(contract_sha256)?;
                        let prior = slots
                            .get(subject)
                            .context("validation subject is missing")?;
                        if subject.position!=dispatch.slot.position || subject.ordinal>=dispatch.slot.ordinal
                            || !matches!(prior.role,Role::Primary|Role::Fallback)
                            || slots.values().any(|d|d.slot!=dispatch.slot && matches!(&d.activation,Activation::Validate{subject:s,..} if s==subject)) {
                            bail!("typed validation subject is not a unique earlier result producer");
                        }
                    }
                    Activation::OnValidationRejection {
                        predecessor,
                        validator,
                    } if dispatch.role == Role::Fallback => {
                        let prior = slots
                            .get(predecessor)
                            .context("rejection fallback subject is missing")?;
                        let validation = slots
                            .get(validator)
                            .context("rejection fallback validator is missing")?;
                        let previous = slots
                            .values()
                            .filter(|d| {
                                d.slot.position == dispatch.slot.position
                                    && d.slot.ordinal < dispatch.slot.ordinal
                                    && matches!(d.role, Role::Primary | Role::Fallback)
                            })
                            .max_by_key(|d| d.slot.ordinal)
                            .context("fallback result chain is missing")?;
                        if predecessor.position != dispatch.slot.position
                            || validator.position != dispatch.slot.position
                            || predecessor.ordinal >= validator.ordinal
                            || validator.ordinal >= dispatch.slot.ordinal
                            || !matches!(prior.role, Role::Primary | Role::Fallback)
                            || validation.role != Role::Validation
                            || !matches!(&validation.activation,Activation::Validate{subject,..} if subject==predecessor)
                            || previous.slot != *predecessor
                        {
                            bail!("rejection fallback does not follow its declared result and typed validator");
                        }
                    }
                    Activation::OnFailure { predecessor } if dispatch.role == Role::Fallback => {
                        let prior = slots
                            .get(predecessor)
                            .context("fallback predecessor is missing")?;
                        if predecessor.position != dispatch.slot.position
                            || predecessor.ordinal >= dispatch.slot.ordinal
                            || !matches!(prior.role, Role::Primary | Role::Fallback)
                        {
                            bail!(
                                "fallback predecessor is not an earlier result-producing dispatch"
                            );
                        }
                        // Every fallback must continue the result chain. An
                        // auxiliary call cannot become a node's returned value.
                        let previous_result = slots
                            .values()
                            .filter(|d| {
                                d.slot.position == dispatch.slot.position
                                    && d.slot.ordinal < dispatch.slot.ordinal
                                    && matches!(d.role, Role::Primary | Role::Fallback)
                            })
                            .max_by_key(|d| d.slot.ordinal)
                            .context("fallback result chain is missing")?;
                        if previous_result.slot != *predecessor {
                            bail!("fallback skips its previous result-producing dispatch");
                        }
                    }
                    _ => bail!("physical graph role and activation are incompatible"),
                }
            }
        }
        if next.keys().copied().collect::<BTreeSet<_>>() != nodes.keys().copied().collect() {
            bail!("physical graph does not cover every logical node");
        }
        Ok(())
    }

    pub fn key(&self) -> Result<PlanProfileKey> {
        self.validate()?;
        let mut dispatches = self.dispatches.clone();
        dispatches.sort_by_key(|d| d.slot);
        let primary = self.graph.profile_key()?;
        let dispatches = dispatches
            .iter()
            .map(|d| {
                Ok(serde_json::json!({
                    "slot":d.slot,"role":d.role,"key":d.key()?,"activation":d.activation,
                }))
            })
            .collect::<Result<Vec<_>>>()?;
        let selection = if self
            .dispatches
            .iter()
            .any(|d| matches!(d.activation, Activation::Validate { .. }))
        {
            "first-accepted-primary-or-fallback-v1"
        } else {
            "first-successful-primary-or-fallback-v1"
        };
        let plan = serde_json::json!({"version":VERSION,"logical_key":primary,"dispatches":dispatches,
            "result_selection":selection});
        Ok(PlanProfileKey {
            call_site_version: primary.call_site_version,
            execution_plan_id: crate::execution_plan::ExecutionPlanId::parse(content_hash(
                &canonical_json(&plan),
            ))?,
        })
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Binding {
    pub slot: Slot,
    pub runtime: PlanRuntimeVersion,
    pub request_sha256: String,
    pub transport_sha256: String,
}

/// All request and transport fingerprints are supplied before the first call.
/// No request body, credential, result content or provider invoice is stored.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Prepared {
    pub program: Program,
    pub bindings: Vec<Binding>,
    pub logical_requests: Vec<SourceContract>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub source_contracts: Vec<SourceContract>,
}

impl Prepared {
    pub fn validate(&self) -> Result<()> {
        self.program.validate()?;
        let slots: BTreeMap<_, _> = self
            .program
            .dispatches
            .iter()
            .map(|d| (d.slot, d))
            .collect();
        let bindings: BTreeSet<_> = self.bindings.iter().map(|b| b.slot).collect();
        let nodes: BTreeSet<_> = self
            .program
            .graph
            .calls
            .branches
            .iter()
            .map(|b| b.position)
            .collect();
        if bindings.len() != self.bindings.len()
            || bindings != slots.keys().copied().collect()
            || self.logical_requests.len() != nodes.len()
            || self
                .logical_requests
                .iter()
                .map(|r| r.position)
                .collect::<BTreeSet<_>>()
                != nodes
        {
            bail!("physical graph bindings do not cover its complete schedule");
        }
        for binding in &self.bindings {
            let plan = &slots[&binding.slot].plan;
            if binding.runtime.provider_protocol != plan.provider_protocol
                || binding.runtime.target_model_id != plan.target_model_id
                || binding.runtime.target_model_version.trim().is_empty()
                || binding.runtime.price_table_version.trim().is_empty()
            {
                bail!("physical graph runtime differs from its dispatch plan");
            }
            for hash in [&binding.request_sha256, &binding.transport_sha256] {
                crate::execution_plan::ExecutionPlanId::parse(hash)?;
            }
        }
        for request in &self.logical_requests {
            crate::execution_plan::ExecutionPlanId::parse(&request.sha256)?;
        }
        if !self.source_contracts.is_empty() {
            if self.source_contracts.len() != nodes.len()
                || self
                    .source_contracts
                    .iter()
                    .map(|s| s.position)
                    .collect::<BTreeSet<_>>()
                    != nodes
            {
                bail!("role graph source controls do not cover every logical node");
            }
            for source in &self.source_contracts {
                crate::execution_plan::ExecutionPlanId::parse(&source.sha256)?;
            }
        }
        Ok(())
    }

    fn canonical(&self) -> Result<String> {
        self.validate()?;
        let mut sorted = self.clone();
        sorted.program.dispatches.sort_by_key(|d| d.slot);
        sorted.bindings.sort_by_key(|b| b.slot);
        sorted.logical_requests.sort_by_key(|r| r.position);
        sorted.source_contracts.sort_by_key(|r| r.position);
        encode(&sorted)
    }

    pub fn identity(&self) -> Result<String> {
        Ok(content_hash(&self.canonical()?))
    }

    pub fn runtime(&self) -> Result<PlanRuntimeVersion> {
        self.validate()?;
        let mut bindings = self.bindings.iter().collect::<Vec<_>>();
        bindings.sort_by_key(|b| b.slot);
        Ok(PlanRuntimeVersion {
            provider_protocol: RUNTIME_VERSION.into(),
            target_model_id: encode(
                &bindings
                    .iter()
                    .map(|b| (&b.slot, &b.runtime.target_model_id))
                    .collect::<Vec<_>>(),
            )?,
            target_model_version: encode(
                &bindings
                    .iter()
                    .map(|b| (&b.slot, &b.runtime))
                    .collect::<Vec<_>>(),
            )?,
            price_table_version: encode(
                &bindings
                    .iter()
                    .map(|b| (&b.slot, &b.runtime.price_table_version))
                    .collect::<Vec<_>>(),
            )?,
        })
    }
}

fn encode(value: &impl Serialize) -> Result<String> {
    Ok(canonical_json(&serde_json::to_value(value)?))
}

pub fn ensure_schema(conn: &Connection) -> Result<()> {
    conn.execute_batch("CREATE TABLE IF NOT EXISTS provider_graph_role_preparation (
        lease_id TEXT PRIMARY KEY,batch_id TEXT NOT NULL UNIQUE,prepared_sha256 TEXT NOT NULL,prepared_json TEXT NOT NULL,
        FOREIGN KEY(lease_id) REFERENCES provider_graph_preparation(lease_id),FOREIGN KEY(batch_id) REFERENCES provider_receipt_batch(batch_id));
        CREATE TABLE IF NOT EXISTS provider_graph_role_execution (
        execution_id TEXT PRIMARY KEY,lease_id TEXT NOT NULL UNIQUE,execution_sha256 TEXT NOT NULL,execution_json TEXT NOT NULL,
        FOREIGN KEY(lease_id) REFERENCES provider_graph_role_preparation(lease_id));
        CREATE TABLE IF NOT EXISTS provider_graph_role_execution_state (
        call_site_version TEXT NOT NULL,execution_plan_id TEXT NOT NULL,
        runtime_json TEXT NOT NULL,source_sequence INTEGER NOT NULL,
        observation_floor INTEGER NOT NULL,window_size INTEGER NOT NULL,
        lifetime_executions INTEGER NOT NULL,
        PRIMARY KEY(call_site_version,execution_plan_id));")?;
    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS provider_graph_validation_verdict (
        invocation_id TEXT PRIMARY KEY,subject_invocation_id TEXT NOT NULL,
        verdict_sha256 TEXT NOT NULL,verdict_json TEXT NOT NULL,
        FOREIGN KEY(invocation_id) REFERENCES provider_receipt_invocation(invocation_id),
        FOREIGN KEY(subject_invocation_id) REFERENCES provider_receipt_invocation(invocation_id));",
    )?;
    Ok(())
}

pub(crate) fn enroll(conn: &Connection, lease: &Lease, prepared: &Prepared) -> Result<()> {
    ensure_schema(conn)?;
    let graph = &prepared.program.graph.calls;
    let metadata: String = conn.query_row(
        "SELECT metadata_json FROM provider_receipt_batch WHERE batch_id=?1",
        [&lease.batch_id],
        |r| r.get(0),
    )?;
    let batch: crate::provider_receipts::Batch = serde_json::from_str(&metadata)?;
    if lease.template.is_some()
        || lease.comparison_context.is_some()
        || lease.input_sha256.is_none()
        || batch.application_version.as_ref() != Some(&graph.application_version)
        || batch.callback_version != graph.callback_version
        || batch.item_count != graph.item_count
        || lease.max_workers != graph.max_workers
        || lease.role_program_id.as_deref() != Some(prepared.identity()?.as_str())
    {
        bail!("prepared role graph belongs to another driver or unsupported comparison cohort");
    }
    let raw = prepared.canonical()?;
    conn.execute(
        "INSERT INTO provider_graph_role_preparation VALUES(?1,?2,?3,?4)",
        params![lease.lease_id, lease.batch_id, content_hash(&raw), raw],
    )?;
    Ok(())
}

pub(crate) fn original(conn: &Connection, lease: &Lease) -> Result<Option<Prepared>> {
    let Some(id) = &lease.role_program_id else {
        return Ok(None);
    };
    let (batch,hash,raw):(String,String,String)=conn.query_row("SELECT batch_id,prepared_sha256,prepared_json FROM provider_graph_role_preparation WHERE lease_id=?1",[&lease.lease_id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?))).context("prepared role graph custody is missing")?;
    let prepared: Prepared = serde_json::from_str(&raw)?;
    if batch != lease.batch_id
        || content_hash(&raw) != hash
        || prepared.identity()? != *id
        || prepared.canonical()? != raw
    {
        bail!("prepared role graph custody is invalid");
    }
    Ok(Some(prepared))
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum VerdictDecision {
    Accepted,
    Rejected,
    Unknown,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ValidationVerdict {
    pub version: String,
    pub invocation_id: String,
    pub subject_invocation_id: String,
    pub contract_sha256: String,
    pub subject_receipt_sha256: String,
    pub validator_receipt_sha256: String,
    pub decision: VerdictDecision,
}
pub const VERDICT_VERSION: &str = "provider-graph-validation-verdict-v1";

type Receipts = BTreeMap<Slot, (Invocation, Option<Completion>, Option<ValidationVerdict>)>;

fn completion_hash(call: &Invocation, done: &Completion) -> Result<String> {
    Ok(content_hash(&encode(
        &serde_json::json!({"invocation":call,"completion":done}),
    )?))
}

fn load_verdict(
    conn: &Connection,
    dispatch: &Dispatch,
    call: &Invocation,
    done: Option<&Completion>,
    calls: &Receipts,
) -> Result<Option<ValidationVerdict>> {
    let Activation::Validate {
        subject,
        contract_sha256,
    } = &dispatch.activation
    else {
        return Ok(None);
    };
    let stored:Option<(String,String,String)>=conn.query_row("SELECT subject_invocation_id,verdict_sha256,verdict_json FROM provider_graph_validation_verdict WHERE invocation_id=?1",[&call.invocation_id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?))).optional()?;
    let Some((id, hash, raw)) = stored else {
        return Ok(None);
    };
    let verdict: ValidationVerdict = serde_json::from_str(&raw)?;
    let (producer, completed, _) = calls
        .get(subject)
        .context("typed verdict subject is missing")?;
    let completed = completed
        .as_ref()
        .context("typed verdict subject is pending")?;
    let done = done.context("typed verdict validator is pending")?;
    if content_hash(&raw) != hash
        || encode(&verdict)? != raw
        || verdict.version != VERDICT_VERSION
        || id != producer.invocation_id
        || verdict.subject_invocation_id != id
        || verdict.invocation_id != call.invocation_id
        || verdict.contract_sha256 != *contract_sha256
        || completed.status != Status::Completed
        || done.status != Status::Completed
        || verdict.subject_receipt_sha256 != completion_hash(producer, completed)?
        || verdict.validator_receipt_sha256 != completion_hash(call, done)?
    {
        bail!("typed graph validation verdict differs from its original receipts or contract");
    }
    Ok(Some(verdict))
}

/// Commit a typed interpretation of the declared validator's original response.
/// This records no new physical work and supplies no reference quality sample.
pub fn record_validation(
    conn: &mut Connection,
    lease_id: &str,
    invocation_id: &str,
    decision: VerdictDecision,
) -> Result<Option<ValidationVerdict>> {
    ensure_schema(conn)?;
    let tx = conn.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
    let lease = crate::provider_graph_preparation::load(&tx, lease_id)?;
    let prepared =
        original(&tx, &lease)?.context("validation verdict requires a prepared role graph")?;
    let calls = receipts(&tx, &prepared, &lease.batch_id)?;
    let (slot, (call, done, old)) = calls
        .iter()
        .find(|(_, entry)| entry.0.invocation_id == invocation_id)
        .context("validator invocation is missing")?;
    let dispatch = prepared
        .program
        .dispatches
        .iter()
        .find(|d| d.slot == *slot)
        .unwrap();
    let Activation::Validate {
        subject,
        contract_sha256,
    } = &dispatch.activation
    else {
        if dispatch.role != Role::Validation {
            bail!("verdict invocation is not a validator");
        }
        tx.commit()?;
        return Ok(None);
    };
    if let Some(old) = old {
        if old.decision != decision {
            bail!("typed validation verdict is already bound to another decision");
        }
        let value = old.clone();
        tx.commit()?;
        return Ok(Some(value));
    }
    let sealed: bool = tx.query_row(
        "SELECT sealed FROM provider_receipt_batch WHERE batch_id=?1",
        [&lease.batch_id],
        |r| r.get(0),
    )?;
    if sealed {
        bail!("new validation verdict cannot be attached after foreground sealing");
    }
    let done = done.as_ref().context("validator receipt is pending")?;
    verify_terminal(&prepared, *slot, done)?;
    let (producer, completed, _) = calls
        .get(subject)
        .context("validation subject is missing")?;
    let completed = completed
        .as_ref()
        .context("validation subject is pending")?;
    verify_terminal(&prepared, *subject, completed)?;
    if completed.status != Status::Completed || done.status != Status::Completed {
        bail!("typed validation requires completed subject and validator receipts");
    }
    let value = ValidationVerdict {
        version: VERDICT_VERSION.into(),
        invocation_id: invocation_id.into(),
        subject_invocation_id: producer.invocation_id.clone(),
        contract_sha256: contract_sha256.clone(),
        subject_receipt_sha256: completion_hash(producer, completed)?,
        validator_receipt_sha256: completion_hash(call, done)?,
        decision,
    };
    let raw = encode(&value)?;
    tx.execute(
        "INSERT INTO provider_graph_validation_verdict VALUES(?1,?2,?3,?4)",
        params![
            invocation_id,
            producer.invocation_id,
            content_hash(&raw),
            raw
        ],
    )?;
    tx.commit()?;
    Ok(Some(value))
}

fn receipts(conn: &Connection, prepared: &Prepared, batch: &str) -> Result<Receipts> {
    let mut q=conn.prepare("SELECT invocation_json,completion_json,item,logical_call,ordinal FROM provider_receipt_invocation WHERE batch_id=?1 ORDER BY item,logical_call,ordinal")?;
    let rows = q
        .query_map([batch], |r| {
            Ok((
                r.get::<_, String>(0)?,
                r.get::<_, Option<String>>(1)?,
                r.get::<_, u32>(2)?,
                r.get::<_, u32>(3)?,
                r.get::<_, u32>(4)?,
            ))
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    let mut result = BTreeMap::new();
    for (raw, done, item, logical_call, ordinal) in rows {
        let call: Invocation = serde_json::from_str(&raw)?;
        let expected = result
            .keys()
            .filter(|s: &&Slot| s.position == call.position)
            .count();
        if call.position
            != (CallPosition {
                item,
                call: logical_call,
            })
            || call.ordinal != ordinal
            || ordinal as usize != expected
        {
            bail!("physical graph receipt order or position is invalid");
        }
        let slot = next_slot(prepared, &result, call.position)?;
        verify_call(prepared, &call, slot)?;
        let done: Option<Completion> = done.map(|v| serde_json::from_str(&v)).transpose()?;
        let dispatch = prepared
            .program
            .dispatches
            .iter()
            .find(|d| d.slot == slot)
            .unwrap();
        let verdict = load_verdict(conn, dispatch, &call, done.as_ref(), &result)?;
        if result.insert(slot, (call, done, verdict)).is_some() {
            bail!("physical graph receipt slots conflict");
        }
    }
    Ok(result)
}

fn next_slot(prepared: &Prepared, calls: &Receipts, position: CallPosition) -> Result<Slot> {
    let mut ordered: Vec<_> = prepared
        .program
        .dispatches
        .iter()
        .filter(|d| d.slot.position == position)
        .collect();
    ordered.sort_by_key(|d| d.slot);
    for dispatch in ordered {
        if !calls.contains_key(&dispatch.slot) && active(dispatch, calls)? {
            return Ok(dispatch.slot);
        }
    }
    bail!("physical graph schedule is exhausted or has no such node")
}

fn active(dispatch: &Dispatch, calls: &Receipts) -> Result<bool> {
    Ok(match &dispatch.activation {
        Activation::Always => true,
        Activation::OnFailure { predecessor } => {
            match calls.get(predecessor).and_then(|(_, c, _)| c.as_ref()) {
                Some(c) => c.status == Status::Failed,
                None if calls.contains_key(predecessor) => {
                    bail!("fallback predecessor is still pending")
                }
                None => false,
            }
        }
        Activation::Validate { subject, .. } => {
            match calls.get(subject).and_then(|(_, c, _)| c.as_ref()) {
                Some(c) => c.status == Status::Completed,
                None if calls.contains_key(subject) => bail!("validation subject is still pending"),
                None => false,
            }
        }
        Activation::OnValidationRejection {
            predecessor,
            validator,
        } => match calls.get(predecessor).and_then(|(_, c, _)| c.as_ref()) {
            Some(c) if c.status == Status::Completed => {
                let verdict = calls
                    .get(validator)
                    .and_then(|(_, _, v)| v.as_ref())
                    .context("fallback awaits its typed validation verdict")?;
                match verdict.decision {
                    VerdictDecision::Rejected => true,
                    VerdictDecision::Accepted => false,
                    VerdictDecision::Unknown => {
                        bail!("unknown validation verdict cannot authorize fallback")
                    }
                }
            }
            Some(_) => false,
            None if calls.contains_key(predecessor) => bail!("fallback subject is still pending"),
            None => false,
        },
    })
}

fn selected(prepared: &Prepared, calls: &Receipts, position: CallPosition) -> Result<Slot> {
    let mut candidates: Vec<_> = prepared
        .program
        .dispatches
        .iter()
        .filter(|d| d.slot.position == position && matches!(d.role, Role::Primary | Role::Fallback))
        .collect();
    candidates.sort_by_key(|d| d.slot);
    for dispatch in candidates {
        if active(dispatch, calls)? {
            let done = calls
                .get(&dispatch.slot)
                .and_then(|(_, c, _)| c.as_ref())
                .context("logical graph result is pending or missing")?;
            if done.status == Status::Completed {
                if let Some(validation)=prepared.program.dispatches.iter().find(|d|matches!(&d.activation,Activation::Validate{subject,..} if *subject==dispatch.slot)) {
                    let verdict=calls.get(&validation.slot).and_then(|(_,_,v)|v.as_ref()).context("logical result awaits its typed validation verdict")?;
                    match verdict.decision {VerdictDecision::Accepted=>{},VerdictDecision::Rejected=>continue,VerdictDecision::Unknown=>bail!("unknown validation verdict cannot release a logical result")}
                }
                return Ok(dispatch.slot);
            }
            if done.status == Status::Cancelled {
                bail!("cancelled dispatch cannot select a fallback result");
            }
        }
    }
    bail!("logical graph has no successful result-producing dispatch")
}

fn verify_call(prepared: &Prepared, call: &Invocation, slot: Slot) -> Result<()> {
    let dispatch = prepared
        .program
        .dispatches
        .iter()
        .find(|d| d.slot == slot)
        .context("physical dispatch is absent from the prepared schedule")?;
    let binding = prepared
        .bindings
        .iter()
        .find(|b| b.slot == slot)
        .context("physical dispatch binding is absent")?;
    let primary = prepared
        .program
        .graph
        .calls
        .branches
        .iter()
        .find(|b| b.position == slot.position)
        .context("physical dispatch has no logical owner")?;
    let owner = PlanProfileKey {
        call_site_version: primary.call_site_version.clone(),
        execution_plan_id: primary.plan.execution_plan_id()?,
    };
    let owner_runtime = &prepared
        .bindings
        .iter()
        .find(|b| {
            b.slot
                == Slot {
                    position: slot.position,
                    ordinal: 0,
                }
        })
        .unwrap()
        .runtime;
    let logical = &prepared
        .logical_requests
        .iter()
        .find(|r| r.position == slot.position)
        .unwrap()
        .sha256;
    let (protocol, model) = if dispatch.plan.provider_protocol == "litellm.completion.v1" {
        (
            "openai.chat.completions.v1",
            dispatch
                .plan
                .target_model_id
                .strip_prefix("openai/")
                .context("role graph LiteLLM backend is unsupported")?,
        )
    } else {
        (
            dispatch.plan.provider_protocol.as_str(),
            dispatch.plan.target_model_id.as_str(),
        )
    };
    if call.role != dispatch.role
        || call.provider_protocol != protocol
        || call.requested_model_id != model
        || call.logical_plan_key.as_ref() != Some(&owner)
        || call.logical_runtime.as_ref() != Some(owner_runtime)
        || call.dispatch_plan_key.as_ref() != Some(&dispatch.key()?)
        || call.dispatch_runtime.as_ref() != Some(&binding.runtime)
        || call.parent_plan_key.as_ref()
            != if slot.ordinal == 0 {
                None
            } else {
                Some(&owner)
            }
        || call.logical_request_sha256.as_ref() != Some(logical)
        || call.request_sha256.as_ref() != Some(&binding.request_sha256)
        || call.transport_sha256.as_ref() != Some(&binding.transport_sha256)
        || !call.transport_verified
    {
        bail!("physical dispatch differs from its prepared role graph");
    }
    Ok(())
}

pub(crate) fn verify_dispatch(
    conn: &Connection,
    lease: &Lease,
    prepared: &Prepared,
    call: &Invocation,
) -> Result<()> {
    let sealed: bool = conn.query_row(
        "SELECT sealed FROM provider_receipt_batch WHERE batch_id=?1",
        [&lease.batch_id],
        |r| r.get(0),
    )?;
    if sealed {
        bail!("prepared role graph foreground has already sealed");
    }
    let calls = receipts(conn, prepared, &lease.batch_id)?;
    for (slot, (_, done, _)) in &calls {
        if let Some(done) = done {
            verify_terminal(prepared, *slot, done)?;
        }
    }
    if let Some((slot, _)) = calls
        .iter()
        .find(|(_, (prior, _, _))| prior.invocation_id == call.invocation_id)
    {
        verify_call(prepared, call, *slot)?;
        return Ok(());
    }
    if call.ordinal as usize != calls.keys().filter(|s| s.position == call.position).count() {
        bail!("physical graph reservation has a duplicate or missing invocation ordinal");
    }
    let slot = next_slot(prepared, &calls, call.position)?;
    verify_call(prepared, call, slot)?;
    let dispatch = prepared
        .program
        .dispatches
        .iter()
        .find(|d| d.slot == slot)
        .unwrap();
    if !active(dispatch, &calls)? {
        bail!("physical graph conditional dispatch is inactive");
    }
    for prior in prepared
        .program
        .dispatches
        .iter()
        .filter(|d| d.slot.position == slot.position && d.slot.ordinal < slot.ordinal)
    {
        if active(prior, &calls)?
            && calls
                .get(&prior.slot)
                .and_then(|(_, c, _)| c.as_ref())
                .is_none()
        {
            bail!("physical graph dispatch precedes unfinished scheduled work");
        }
    }
    for edge in prepared
        .program
        .graph
        .dependencies
        .iter()
        .filter(|d| d.consumer == slot.position)
    {
        selected(prepared, &calls, edge.producer)?;
        // A logical dependency releases only after all active producer work,
        // including its validation, has completed successfully or fallen back.
        for prior in prepared
            .program
            .dispatches
            .iter()
            .filter(|d| d.slot.position == edge.producer)
        {
            if active(prior, &calls)? {
                let done = calls
                    .get(&prior.slot)
                    .and_then(|(_, c, _)| c.as_ref())
                    .context("physical graph dependency has unfinished auxiliary work")?;
                if !matches!(prior.role, Role::Primary | Role::Fallback)
                    && done.status != Status::Completed
                {
                    bail!("physical graph dependency has failed auxiliary work");
                }
            }
        }
    }
    Ok(())
}

fn verify_terminal(prepared: &Prepared, slot: Slot, done: &Completion) -> Result<()> {
    let dispatch = prepared
        .program
        .dispatches
        .iter()
        .find(|d| d.slot == slot)
        .unwrap();
    let binding = prepared.bindings.iter().find(|b| b.slot == slot).unwrap();
    if done.http_attempts != Some(1)
        || done.status == Status::Cancelled
        || done.status == Status::Completed
            && !crate::model_catalog::reported_revision_matches(
                &binding.runtime,
                done.model_version.as_deref(),
            )
        || !matches!(dispatch.role, Role::Primary | Role::Fallback)
            && done.status != Status::Completed
    {
        bail!("physical graph has an unsupported retry, runtime or failed auxiliary result");
    }
    Ok(())
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SelectedResult {
    pub slot: Slot,
    pub invocation_id: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Execution {
    pub version: String,
    pub execution_id: String,
    pub lease_id: String,
    pub batch_id: String,
    pub prepared_id: String,
    pub key: PlanProfileKey,
    pub runtime: PlanRuntimeVersion,
    pub journal_sha256: String,
    pub started_at_us: i64,
    pub observed_at_us: i64,
    pub selected_results: Vec<SelectedResult>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub validation_sha256: Option<String>,
    pub comparison: crate::provider_graph::Comparison,
    /// This is a validated monetary sidecar at completion, not a reusable
    /// rolling graph profile. Unknown failure charges cannot authorize spend.
    pub acquisition: crate::provider_graph_acquisition::Economics,
}

fn execution_identity(execution: &Execution) -> Result<String> {
    let mut value = serde_json::to_value(execution)?;
    value.as_object_mut().unwrap().remove("execution_id");
    value.as_object_mut().unwrap().remove("observed_at_us");
    Ok(content_hash(&canonical_json(&value)))
}

pub fn finish(
    conn: &mut Connection,
    lease_id: &str,
    comparison: crate::provider_graph::Comparison,
    now: i64,
) -> Result<Execution> {
    ensure_schema(conn)?;
    let tx = conn.transaction()?;
    let lease = crate::provider_graph_preparation::load(&tx, lease_id)?;
    crate::provider_graph_deployment::verify_current(&tx, &lease, now)?;
    crate::provider_graph_role_comparison::verify_context_current(&tx, &lease)?;
    let prepared = original(&tx, &lease)?.context("graph has no prepared role program")?;
    if comparison.version != crate::provider_graph::COMPARISON_VERSION
        || lease.input_sha256.as_ref() != Some(&comparison.input_sha256)
        || now < lease.started_at_us
    {
        bail!("physical graph result contract or clock differs from its original preparation");
    }
    crate::execution_plan::ExecutionPlanId::parse(&comparison.output_sha256)?;
    let acquisition = crate::provider_graph_acquisition::report_batch(&tx, &lease.batch_id)?
        .context("role graph acquisition owner is missing")?;
    if acquisition.unfinished_batches != 0 || acquisition.persistence_failed_batches != 0 {
        bail!("role graph foreground accounting is unfinished or failed");
    }
    let seal: String = tx.query_row(
        "SELECT expected_calls_json FROM provider_receipt_batch WHERE batch_id=?1",
        [&lease.batch_id],
        |r| r.get(0),
    )?;
    let coverage: Vec<CallPosition> = serde_json::from_str(&seal)?;
    let nodes: BTreeSet<_> = prepared
        .program
        .graph
        .calls
        .branches
        .iter()
        .map(|b| b.position)
        .collect();
    if coverage.len() != nodes.len() || coverage.into_iter().collect::<BTreeSet<_>>() != nodes {
        bail!("role graph foreground seal does not cover every logical result");
    }
    let calls = receipts(&tx, &prepared, &lease.batch_id)?;
    for dispatch in &prepared.program.dispatches {
        let is_active = active(dispatch, &calls)?;
        let row = calls.get(&dispatch.slot);
        if is_active != row.is_some() {
            bail!("physical graph receipt coverage differs from its conditional schedule");
        }
        if let Some((call, done, _)) = row {
            verify_call(&prepared, call, dispatch.slot)?;
            let done = done
                .as_ref()
                .context("physical graph dispatch has no terminal receipt")?;
            verify_terminal(&prepared, dispatch.slot, done)?;
        }
    }
    if calls.len()
        != prepared
            .program
            .dispatches
            .iter()
            .filter(|d| calls.contains_key(&d.slot))
            .count()
    {
        bail!("physical graph journal includes undeclared work");
    }
    let mut nodes: Vec<_> = prepared
        .program
        .graph
        .calls
        .branches
        .iter()
        .map(|b| b.position)
        .collect();
    nodes.sort();
    let mut selected_results = vec![];
    for position in nodes {
        let slot = selected(&prepared, &calls, position)?;
        selected_results.push(SelectedResult {
            slot,
            invocation_id: calls[&slot].0.invocation_id.clone(),
        });
    }
    let mut execution = Execution {
        version: VERSION.into(),
        execution_id: String::new(),
        lease_id: lease_id.into(),
        batch_id: lease.batch_id.clone(),
        prepared_id: prepared.identity()?,
        key: prepared.program.key()?,
        runtime: prepared.runtime()?,
        journal_sha256: crate::provider_publication::journal_fingerprint(&tx, &lease.batch_id)?,
        started_at_us: lease.started_at_us,
        observed_at_us: now,
        selected_results,
        validation_sha256: validation_fingerprint(&prepared, &calls)?,
        comparison,
        acquisition,
    };
    execution.execution_id = execution_identity(&execution)?;
    let saved: Option<String> = tx
        .query_row(
            "SELECT execution_json FROM provider_graph_role_execution WHERE lease_id=?1",
            [lease_id],
            |r| r.get(0),
        )
        .optional()?;
    if let Some(raw) = saved {
        let original = load(&tx, &serde_json::from_str::<Execution>(&raw)?.execution_id)?;
        if original.prepared_id != execution.prepared_id
            || original.journal_sha256 != execution.journal_sha256
            || original.comparison != execution.comparison
            || original.selected_results != execution.selected_results
            || original.validation_sha256 != execution.validation_sha256
        {
            bail!("physical graph retry differs from its original completed execution");
        }
        tx.commit()?;
        return Ok(original);
    }
    let raw = encode(&execution)?;
    tx.execute(
        "INSERT INTO provider_graph_role_execution VALUES(?1,?2,?3,?4)",
        params![execution.execution_id, lease_id, content_hash(&raw), raw],
    )?;
    let sequence = tx.last_insert_rowid();
    tx.execute("INSERT INTO provider_graph_role_execution_state VALUES(?1,?2,?3,?4,?4,?5,1)
        ON CONFLICT(call_site_version,execution_plan_id) DO UPDATE SET
        observation_floor=CASE WHEN excluded.runtime_json<>runtime_json OR excluded.window_size<>window_size THEN excluded.observation_floor ELSE observation_floor END,
        runtime_json=excluded.runtime_json,source_sequence=excluded.source_sequence,
        window_size=excluded.window_size,lifetime_executions=lifetime_executions+1",
        params![execution.key.call_site_version.as_str(),execution.key.execution_plan_id.as_str(),encode(&execution.runtime)?,sequence,lease.window_size])?;
    tx.commit()?;
    Ok(execution)
}

pub(crate) fn validate_execution(
    conn: &Connection,
    id: &str,
    hash: &str,
    raw: &str,
    e: &Execution,
    lease: &Lease,
    prepared: &Prepared,
) -> Result<()> {
    if content_hash(raw) != hash
        || e.version != VERSION
        || e.execution_id != id
        || execution_identity(e)? != id
        || e.prepared_id != prepared.identity()?
        || e.key != prepared.program.key()?
        || e.runtime != prepared.runtime()?
        || e.batch_id != lease.batch_id
        || e.started_at_us != lease.started_at_us
        || e.observed_at_us < e.started_at_us
        || e.journal_sha256
            != crate::provider_publication::journal_fingerprint(conn, &lease.batch_id)?
    {
        bail!("completed physical graph custody is invalid");
    }
    if prepared
        .program
        .dispatches
        .iter()
        .any(|d| matches!(d.activation, Activation::Validate { .. }))
    {
        let calls = receipts(conn, prepared, &lease.batch_id)?;
        if e.validation_sha256 != validation_fingerprint(prepared, &calls)? {
            bail!("completed graph validation evidence is missing or changed");
        }
        let mut positions: Vec<_> = prepared
            .program
            .graph
            .calls
            .branches
            .iter()
            .map(|b| b.position)
            .collect();
        positions.sort();
        let expected = positions
            .into_iter()
            .map(|position| -> Result<SelectedResult> {
                let slot = selected(prepared, &calls, position)?;
                Ok(SelectedResult {
                    slot,
                    invocation_id: calls[&slot].0.invocation_id.clone(),
                })
            })
            .collect::<Result<Vec<_>>>()?;
        if e.selected_results != expected {
            bail!("completed graph result coverage or invocation differs from its typed verdict");
        }
    } else if e.validation_sha256.is_some() {
        bail!("legacy graph cannot acquire typed validation evidence");
    }
    Ok(())
}

fn validation_fingerprint(prepared: &Prepared, calls: &Receipts) -> Result<Option<String>> {
    if !prepared
        .program
        .dispatches
        .iter()
        .any(|d| matches!(d.activation, Activation::Validate { .. }))
    {
        return Ok(None);
    }
    let mut verdicts = vec![];
    for d in &prepared.program.dispatches {
        if matches!(d.activation, Activation::Validate { .. }) && active(d, calls)? {
            verdicts.push(
                calls
                    .get(&d.slot)
                    .and_then(|(_, _, v)| v.as_ref())
                    .context("completed graph has no typed validation verdict")?,
            );
        }
    }
    verdicts.sort_by(|a, b| a.invocation_id.cmp(&b.invocation_id));
    Ok(Some(content_hash(&encode(&verdicts)?)))
}

pub fn load(conn: &Connection, id: &str) -> Result<Execution> {
    if conn.is_autocommit() {
        let tx = conn.unchecked_transaction()?;
        let result = crate::provider_graph_archive::Archive::new(&tx)?.execution(id)?;
        tx.commit()?;
        return Ok(result);
    }
    crate::provider_graph_archive::Archive::new(conn)?.execution(id)
}

pub(crate) fn head(conn: &Connection, key: &PlanProfileKey) -> Result<String> {
    let (runtime,floor,size):(String,i64,usize)=conn.query_row("SELECT runtime_json,observation_floor,window_size FROM provider_graph_role_execution_state WHERE call_site_version=?1 AND execution_plan_id=?2",
        params![key.call_site_version.as_str(),key.execution_plan_id.as_str()],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?)))?;
    if floor <= 0 || size == 0 {
        bail!("role graph execution epoch is invalid");
    }
    Ok(content_hash(&encode(
        &serde_json::json!({"runtime":runtime,"floor":floor,"window":size}),
    )?))
}
