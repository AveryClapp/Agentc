//! One original provider execution identity, independent of economic projections.
//!
//! This registry records completed original SDK executions, including their
//! frozen comparison contract. Pairing compares original outputs once. Neither
//! registration nor pairing resolves money or authorizes optimized dispatch.

use anyhow::{bail, Context, Result};
use rusqlite::{params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};

use crate::plan_profile::{PlanProfileKey, PlanRuntimeVersion};
use crate::provider_publication::ComparisonInput;

pub const VERSION: &str = "provider-original-execution-v1";

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PhysicalReceipt {
    pub invocation_id: String,
    pub receipt_sha256: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Execution {
    pub version: String,
    pub execution_id: String,
    pub batch_id: String,
    pub source_sequence: u64,
    pub key: PlanProfileKey,
    pub runtime_version: PlanRuntimeVersion,
    pub observed_at_us: i64,
    pub input_sha256: String,
    pub journal_sha256: String,
    pub receipts: Vec<PhysicalReceipt>,
    pub original_comparison: Option<ComparisonInput>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Pair {
    pub pair_id: String,
    pub reference_execution_id: String,
    pub candidate_execution_id: String,
    pub key: PlanProfileKey,
    pub runtime_version: PlanRuntimeVersion,
    pub divergence: f64,
    pub observed_at_us: i64,
}

pub fn ensure_schema(conn: &Connection) -> Result<()> {
    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS provider_original_execution (
           execution_id TEXT PRIMARY KEY,batch_id TEXT NOT NULL UNIQUE,
           call_site_version TEXT NOT NULL,execution_plan_id TEXT NOT NULL,
           source_sequence INTEGER NOT NULL CHECK(source_sequence>0),
           execution_sha256 TEXT NOT NULL,execution_json TEXT NOT NULL,
           FOREIGN KEY(batch_id) REFERENCES provider_receipt_publication_input(batch_id));
         CREATE INDEX IF NOT EXISTS provider_original_execution_window
           ON provider_original_execution(call_site_version,execution_plan_id,source_sequence DESC);
         CREATE TABLE IF NOT EXISTS provider_original_execution_state (
           call_site_version TEXT NOT NULL,execution_plan_id TEXT NOT NULL,
           source_sequence INTEGER NOT NULL,observation_floor INTEGER NOT NULL,
           runtime_json TEXT NOT NULL,window_size INTEGER NOT NULL CHECK(window_size>0),
           lifetime_executions INTEGER NOT NULL CHECK(typeof(lifetime_executions)='integer' AND lifetime_executions>0),
           PRIMARY KEY(call_site_version,execution_plan_id));
         CREATE TABLE IF NOT EXISTS provider_original_execution_pair (
           pair_id TEXT PRIMARY KEY,reference_execution_id TEXT NOT NULL UNIQUE,
           candidate_execution_id TEXT NOT NULL UNIQUE,call_site_version TEXT NOT NULL,
           execution_plan_id TEXT NOT NULL,candidate_source_sequence INTEGER NOT NULL,
           pair_sha256 TEXT NOT NULL,pair_json TEXT NOT NULL,
           FOREIGN KEY(reference_execution_id) REFERENCES provider_original_execution(execution_id),
           FOREIGN KEY(candidate_execution_id) REFERENCES provider_original_execution(execution_id));
         CREATE INDEX IF NOT EXISTS provider_original_pair_window
           ON provider_original_execution_pair(call_site_version,execution_plan_id,candidate_source_sequence DESC);
         CREATE TABLE IF NOT EXISTS provider_original_usd_comparison (
           token TEXT PRIMARY KEY,execution_id TEXT NOT NULL UNIQUE,
           FOREIGN KEY(execution_id) REFERENCES provider_original_execution(execution_id));",
    )?;
    // USD comparison bindings are a rolling window. Keep their original
    // identity mappings as retirement proofs without pinning evicted bindings.
    let pins_window: bool = conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM pragma_foreign_key_list('provider_original_usd_comparison') WHERE \"table\"='multi_call_comparison_execution')",
        [], |row| row.get(0),
    )?;
    if pins_window {
        conn.execute_batch("SAVEPOINT original_usd_mapping_upgrade;")?;
        let upgraded = conn.execute_batch(
            "CREATE TABLE provider_original_usd_comparison_upgrade (
               token TEXT PRIMARY KEY,execution_id TEXT NOT NULL UNIQUE,
               FOREIGN KEY(execution_id) REFERENCES provider_original_execution(execution_id));
             INSERT INTO provider_original_usd_comparison_upgrade SELECT token,execution_id FROM provider_original_usd_comparison;
             DROP TABLE provider_original_usd_comparison;
             ALTER TABLE provider_original_usd_comparison_upgrade RENAME TO provider_original_usd_comparison;",
        );
        if let Err(error) = upgraded {
            conn.execute_batch(
                "ROLLBACK TO original_usd_mapping_upgrade; RELEASE original_usd_mapping_upgrade;",
            )?;
            return Err(error.into());
        }
        conn.execute_batch("RELEASE original_usd_mapping_upgrade;")?;
    }
    Ok(())
}

fn canonical<T: Serialize>(value: &T) -> Result<String> {
    Ok(agentc_core::storage::canonical_json(&serde_json::to_value(
        value,
    )?))
}

fn identity(batch: &str, input: &str, journal: &str) -> String {
    agentc_core::storage::content_hash(&agentc_core::storage::canonical_json(
        &serde_json::json!({"version":VERSION,"batch_id":batch,"input_sha256":input,"journal_sha256":journal}),
    ))
}

pub(crate) fn receipt_proofs(conn: &Connection, batch: &str) -> Result<Vec<PhysicalReceipt>> {
    let mut query = conn.prepare(
        "SELECT invocation_id,invocation_json,completion_json FROM provider_receipt_invocation WHERE batch_id=?1 ORDER BY item,logical_call,ordinal",
    )?;
    let rows = query.query_map([batch], |r| {
        Ok((
            r.get::<_, String>(0)?,
            r.get::<_, String>(1)?,
            r.get::<_, String>(2)?,
        ))
    })?;
    let mut proofs = Vec::new();
    for row in rows {
        let (id, raw, done) = row?;
        let binding = serde_json::json!({"version":"provider-billing-binding-v1","batch_id":batch,
            "invocation_id":id,"invocation":raw,"completion":done});
        proofs.push(PhysicalReceipt {
            invocation_id: id,
            receipt_sha256: agentc_core::storage::content_hash(&canonical(&binding)?),
        });
    }
    Ok(proofs)
}

fn source(conn: &Connection, batch: &str) -> Result<Option<(Execution, usize)>> {
    let present: bool = conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM provider_receipt_publication_input WHERE batch_id=?1)",
        [batch],
        |r| r.get(0),
    )?;
    if !present {
        return Ok(None);
    }
    let input = crate::provider_publication::load_input(conn, batch)?;
    let Some(evidence) = input.original_graph() else {
        return Ok(None);
    };
    if evidence.spec.branches.iter().any(|branch| {
        branch.plan.target_model_id != branch.plan.requested_model_id
            || !branch.plan.rewrites.is_empty()
            || branch.plan.cache_policy.stable_name != "provider-call"
    }) {
        bail!("original execution comparison requires the original provider graph");
    }
    if let Some(comparison) = &input.comparison {
        let mut reference = evidence.spec.clone();
        reference.max_workers = 1;
        if evidence.spec.comparison_version.as_deref()
            != Some(crate::multi_call::EXACT_MAP_COMPARISON_VERSION)
            || comparison.max_workers != evidence.spec.max_workers
            || comparison.reference_plan_id
                != reference
                    .profile_key()
                    .map_err(anyhow::Error::msg)?
                    .execution_plan_id
        {
            bail!("original comparison differs from its complete graph");
        }
        crate::execution_plan::ExecutionPlanId::parse(comparison.input_fingerprint.clone())?;
        crate::execution_plan::ExecutionPlanId::parse(comparison.output_fingerprint.clone())?;
    }
    let requests = evidence.request_fingerprints.iter().cloned().collect();
    crate::provider_receipts::verify_original_evidence(
        conn,
        batch,
        &evidence.spec,
        &input.totals,
        &requests,
        false,
    )?;
    if crate::provider_publication::journal_fingerprint(conn, batch)? != input.journal_sha256 {
        bail!("original execution journal differs from its checkpoint");
    }
    let (hash, sequence): (String, u64) = conn.query_row(
        "SELECT input_sha256,rowid FROM provider_receipt_publication_input WHERE batch_id=?1",
        [batch],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    Ok(Some((
        Execution {
            version: VERSION.into(),
            execution_id: identity(batch, &hash, &input.journal_sha256),
            batch_id: batch.into(),
            source_sequence: sequence,
            key: input.update.key,
            runtime_version: input.update.runtime_version,
            observed_at_us: input
                .update
                .observed_at_us
                .context("original execution time is missing")?,
            input_sha256: hash,
            journal_sha256: input.journal_sha256,
            receipts: receipt_proofs(conn, batch)?,
            original_comparison: input.comparison,
        },
        input.retained_window_size,
    )))
}

fn register_in_transaction(conn: &Connection, batch: &str) -> Result<Option<Execution>> {
    let existing: Option<String> = conn
        .query_row(
            "SELECT execution_id FROM provider_original_execution WHERE batch_id=?1",
            [batch],
            |r| r.get(0),
        )
        .optional()?;
    if let Some(id) = existing {
        let execution = load(conn, &id)?;
        recover_usd_binding(conn, &execution)?;
        return Ok(Some(execution));
    }
    let Some((execution, size)) = source(conn, batch)? else {
        return Ok(None);
    };
    let json = canonical(&execution)?;
    let inserted = conn.execute(
        "INSERT INTO provider_original_execution VALUES(?1,?2,?3,?4,?5,?6,?7) ON CONFLICT DO NOTHING",
        params![execution.execution_id,batch,execution.key.call_site_version.as_str(),execution.key.execution_plan_id.as_str(),
            execution.source_sequence,agentc_core::storage::content_hash(&json),json],
    )?;
    let old: (String, String) = conn.query_row(
        "SELECT execution_id,execution_json FROM provider_original_execution WHERE batch_id=?1",
        [batch],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    if old != (execution.execution_id.clone(), json) {
        bail!("original execution identity conflicts")
    }
    if inserted == 1 {
        let runtime = canonical(&execution.runtime_version)?;
        conn.execute(
            "INSERT INTO provider_original_execution_state VALUES(?1,?2,?3,?3,?4,?5,1)
             ON CONFLICT(call_site_version,execution_plan_id) DO UPDATE SET lifetime_executions=lifetime_executions+1,
             observation_floor=CASE WHEN excluded.source_sequence>source_sequence AND (excluded.runtime_json<>runtime_json OR excluded.window_size<>window_size) THEN excluded.observation_floor ELSE observation_floor END,
             runtime_json=CASE WHEN excluded.source_sequence>source_sequence THEN excluded.runtime_json ELSE runtime_json END,
             window_size=CASE WHEN excluded.source_sequence>source_sequence THEN excluded.window_size ELSE window_size END,
             source_sequence=MAX(source_sequence,excluded.source_sequence)",
            params![execution.key.call_site_version.as_str(),execution.key.execution_plan_id.as_str(),execution.source_sequence,runtime,size],
        )?;
    }
    recover_usd_binding(conn, &execution)?;
    Ok(Some(execution))
}

fn recover_usd_binding(conn: &Connection, execution: &Execution) -> Result<()> {
    let result: Option<String> = conn.query_row(
        "SELECT result_json FROM provider_receipt_profile_publication WHERE batch_id=?1",
        [&execution.batch_id],
        |r| r.get(0),
    )?;
    if let Some(result) = result {
        let result: serde_json::Value = serde_json::from_str(&result)?;
        if result["status"] == "recorded" {
            if let Some(token) = result["comparison_token"].as_str() {
                let stored: Option<String> = conn
                    .query_row(
                        "SELECT token FROM provider_original_usd_comparison WHERE execution_id=?1",
                        [&execution.execution_id],
                        |r| r.get(0),
                    )
                    .optional()?;
                if stored.as_deref() == Some(token) {
                    return Ok(());
                }
                bind_usd_comparison(conn, &execution.execution_id, token)?;
            }
        }
    }
    Ok(())
}

/// Register the same original identity from any economic publisher. Existing
/// caller transactions own the commit; otherwise registration commits once.
pub fn register(conn: &Connection, batch: &str) -> Result<Option<Execution>> {
    ensure_schema(conn)?;
    if conn.is_autocommit() {
        let tx = conn.unchecked_transaction()?;
        let result = register_in_transaction(&tx, batch)?;
        tx.commit()?;
        return Ok(result);
    }
    register_in_transaction(conn, batch)
}

pub(crate) fn load(conn: &Connection, id: &str) -> Result<Execution> {
    let (hash,json): (String,String) = conn.query_row(
        "SELECT execution_sha256,execution_json FROM provider_original_execution WHERE execution_id=?1", [id], |r|Ok((r.get(0)?,r.get(1)?)),
    ).context("original execution identity is unavailable")?;
    if agentc_core::storage::content_hash(&json) != hash {
        bail!("original execution fingerprint is invalid")
    }
    let execution: Execution = serde_json::from_str(&json)?;
    if execution.version != VERSION
        || execution.execution_id != id
        || identity(
            &execution.batch_id,
            &execution.input_sha256,
            &execution.journal_sha256,
        ) != id
    {
        bail!("original execution identity is invalid");
    }
    let original = crate::provider_publication::load_input(conn, &execution.batch_id)?;
    let (hash, sequence): (String, u64) = conn.query_row(
        "SELECT input_sha256,rowid FROM provider_receipt_publication_input WHERE batch_id=?1",
        [&execution.batch_id],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    if original.update.key != execution.key
        || original.update.runtime_version != execution.runtime_version
        || original.update.observed_at_us != Some(execution.observed_at_us)
        || original.journal_sha256 != execution.journal_sha256
        || original.comparison != execution.original_comparison
        || hash != execution.input_sha256
        || sequence != execution.source_sequence
    {
        bail!("original execution no longer matches its original checkpoint");
    }
    for receipt in &execution.receipts {
        let (raw,done): (String,String)=conn.query_row(
            "SELECT invocation_json,completion_json FROM provider_receipt_invocation WHERE batch_id=?1 AND invocation_id=?2",
            params![execution.batch_id,receipt.invocation_id], |r|Ok((r.get(0)?,r.get(1)?)),
        )?;
        let binding = serde_json::json!({"version":"provider-billing-binding-v1","batch_id":execution.batch_id,
            "invocation_id":receipt.invocation_id,"invocation":raw,"completion":done});
        if receipt.receipt_sha256 != agentc_core::storage::content_hash(&canonical(&binding)?) {
            bail!("original execution no longer matches its original SDK receipt");
        }
    }
    Ok(execution)
}

fn current(conn: &Connection, execution: &Execution) -> Result<bool> {
    let state: Option<(u64,String,usize)> = conn.query_row(
        "SELECT observation_floor,runtime_json,window_size FROM provider_original_execution_state WHERE call_site_version=?1 AND execution_plan_id=?2",
        params![execution.key.call_site_version.as_str(),execution.key.execution_plan_id.as_str()],
        |r|Ok((r.get(0)?,r.get(1)?,r.get(2)?)),
    ).optional()?;
    let Some((floor, runtime, size)) = state else {
        return Ok(false);
    };
    if execution.source_sequence < floor || runtime != canonical(&execution.runtime_version)? {
        return Ok(false);
    }
    let retained: bool=conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM (SELECT execution_id FROM provider_original_execution WHERE call_site_version=?1 AND execution_plan_id=?2 AND source_sequence>=?3 ORDER BY source_sequence DESC LIMIT ?4) WHERE execution_id=?5)",
        params![execution.key.call_site_version.as_str(),execution.key.execution_plan_id.as_str(),floor,size,execution.execution_id], |r|r.get(0),
    )?;
    Ok(retained)
}

/// Compare exactly one reference and one candidate original result. Economic
/// projections never appear in the binding, pair identity or divergence.
pub fn compare(conn: &mut Connection, reference: &str, candidate: &str, at: i64) -> Result<Pair> {
    ensure_schema(conn)?;
    let tx = conn.transaction()?;
    let pair = compare_in_transaction(&tx, reference, candidate, at)?;
    tx.commit()?;
    Ok(pair)
}

fn compare_in_transaction(
    conn: &Connection,
    reference: &str,
    candidate: &str,
    at: i64,
) -> Result<Pair> {
    let reference = load(conn, reference)?;
    let candidate = load(conn, candidate)?;
    let r = reference
        .original_comparison
        .as_ref()
        .context("original reference results are unavailable")?;
    let c = candidate
        .original_comparison
        .as_ref()
        .context("original candidate results are unavailable")?;
    if reference.execution_id == candidate.execution_id
        || r.max_workers != 1
        || c.max_workers <= 1
        || reference.key.call_site_version != candidate.key.call_site_version
        || reference.key.execution_plan_id != c.reference_plan_id
        || reference.runtime_version != candidate.runtime_version
        || r.input_fingerprint != c.input_fingerprint
        || at < reference.observed_at_us
        || at < candidate.observed_at_us
        || !current(conn, &reference)?
        || !current(conn, &candidate)?
    {
        bail!(
            "original comparison inputs, reference, runtime, time or retained window do not match"
        );
    }
    let pair_id = agentc_core::storage::content_hash(&canonical(&serde_json::json!({
        "version":"provider-original-pair-v1","reference":reference.execution_id,"candidate":candidate.execution_id}))?);
    let legacy_time = legacy_pair_time(conn, &reference, &candidate)?;
    if legacy_time.is_some_and(|time| time > at) {
        bail!("original comparison predates its committed comparison");
    }
    let used: Option<(String,String,String)>=conn.query_row(
        "SELECT pair_id,pair_sha256,pair_json FROM provider_original_execution_pair WHERE reference_execution_id=?1 OR candidate_execution_id=?2",
        params![reference.execution_id,candidate.execution_id], |r|Ok((r.get(0)?,r.get(1)?,r.get(2)?)),
    ).optional()?;
    if let Some((id, hash, json)) = used {
        if id != pair_id || agentc_core::storage::content_hash(&json) != hash {
            bail!("original execution already belongs to another pair or its pair is invalid")
        }
        let pair: Pair = serde_json::from_str(&json)?;
        if pair.pair_id != pair_id
            || pair.reference_execution_id != reference.execution_id
            || pair.candidate_execution_id != candidate.execution_id
            || pair.key != candidate.key
            || pair.runtime_version != candidate.runtime_version
            || pair.divergence != f64::from(r.output_fingerprint != c.output_fingerprint)
            || pair.observed_at_us < reference.observed_at_us
            || pair.observed_at_us < candidate.observed_at_us
            || legacy_time.is_some_and(|time| time != pair.observed_at_us)
        {
            bail!("original pair conflicts with its original execution bindings");
        }
        crate::provider_quality::record(conn, &pair)?;
        return Ok(pair);
    }
    let pair = Pair {
        pair_id,
        reference_execution_id: reference.execution_id,
        candidate_execution_id: candidate.execution_id,
        key: candidate.key,
        runtime_version: candidate.runtime_version,
        divergence: f64::from(r.output_fingerprint != c.output_fingerprint),
        observed_at_us: legacy_time.unwrap_or(at),
    };
    let json = canonical(&pair)?;
    conn.execute(
        "INSERT INTO provider_original_execution_pair VALUES(?1,?2,?3,?4,?5,?6,?7,?8)",
        params![
            pair.pair_id,
            pair.reference_execution_id,
            pair.candidate_execution_id,
            pair.key.call_site_version.as_str(),
            pair.key.execution_plan_id.as_str(),
            candidate.source_sequence,
            agentc_core::storage::content_hash(&json),
            json
        ],
    )?;
    crate::provider_quality::record(conn, &pair)?;
    Ok(pair)
}

/// Link the verified-USD view to the original execution in the publisher's
/// transaction. Monetary values never participate in the original identity.
pub(crate) fn bind_usd_comparison(
    conn: &Connection,
    execution_id: &str,
    token: &str,
) -> Result<()> {
    let execution = load(conn, execution_id)?;
    let original = execution
        .original_comparison
        .as_ref()
        .context("original comparison is unavailable")?;
    let raw: String = conn.query_row(
        "SELECT binding_json FROM multi_call_comparison_execution WHERE token=?1",
        [token],
        |r| r.get(0),
    )?;
    let binding: crate::multi_call_comparison::ComparisonBinding = serde_json::from_str(&raw)?;
    if agentc_core::storage::content_hash(&canonical(&binding)?) != token
        || binding.comparison_version != crate::multi_call::EXACT_MAP_COMPARISON_VERSION
        || binding.observation.key() != &execution.key
        || binding.observation.runtime_version() != &execution.runtime_version
        || binding.reference_plan_id != original.reference_plan_id
        || binding.max_workers != original.max_workers
        || binding.input_fingerprint != original.input_fingerprint
        || binding.output_fingerprint != original.output_fingerprint
        || binding.observed_at_us != execution.observed_at_us
    {
        bail!("verified-USD comparison differs from its original execution");
    }
    conn.execute(
        "INSERT INTO provider_original_usd_comparison VALUES(?1,?2) ON CONFLICT DO NOTHING",
        params![token, execution_id],
    )?;
    let stored: String = conn.query_row(
        "SELECT execution_id FROM provider_original_usd_comparison WHERE token=?1",
        [token],
        |r| r.get(0),
    )?;
    if stored != execution_id {
        bail!("verified-USD comparison conflicts with its original identity");
    }
    Ok(())
}

/// Upgrade old completed publications without editing their results or
/// consuming their outputs again. Retired legacy pairs stay retired.
fn legacy_pair_time(
    conn: &Connection,
    reference: &Execution,
    candidate: &Execution,
) -> Result<Option<i64>> {
    type Usage = (String, Option<String>, Option<String>);
    let usage = |execution: &Execution| -> Result<Option<Usage>> {
        let token: Option<String> = conn
            .query_row(
                "SELECT token FROM provider_original_usd_comparison WHERE execution_id=?1",
                [&execution.execution_id],
                |r| r.get(0),
            )
            .optional()?;
        token.map(|token| -> Result<Usage> {
            let (candidate,reference) = conn.query_row(
                "SELECT used_as_candidate,used_as_reference FROM multi_call_comparison_execution WHERE token=?1",
                [&token], |r|Ok((r.get(0)?,r.get(1)?)),
            ).context("original execution's USD binding has retired")?;
            Ok((token,candidate,reference))
        }).transpose()
    };
    let (r, c) = (usage(reference)?, usage(candidate)?);
    let used = |row: &Option<Usage>| {
        row.as_ref()
            .is_some_and(|(_, candidate, reference)| candidate.is_some() || reference.is_some())
    };
    if !used(&r) && !used(&c) {
        return Ok(None);
    }
    let (Some((rt, rc, rr)), Some((ct, cc, cr))) = (r, c) else {
        bail!("original execution already belongs to a verified-USD pair");
    };
    let expected = agentc_core::storage::content_hash(&canonical(&serde_json::json!({
        "version":crate::multi_call::EXACT_MAP_COMPARISON_VERSION,"reference":rt,"candidate":ct,
    }))?);
    if rc.is_some()
        || cr.is_some()
        || rr.as_deref() != Some(&expected)
        || cc.as_deref() != Some(&expected)
    {
        bail!("original execution already belongs to another verified-USD pair");
    }
    let legacy: Option<(String,String,f64,i64)> = conn.query_row(
        "SELECT reference_token,candidate_token,divergence,observed_at FROM multi_call_comparison_pair WHERE pair_id=?1",
        [&expected], |r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?)),
    ).optional()?;
    let (stored_reference, stored_candidate, divergence, time) =
        legacy.context("original execution's verified-USD pair has retired")?;
    let r = reference
        .original_comparison
        .as_ref()
        .context("original reference results are unavailable")?;
    let c = candidate
        .original_comparison
        .as_ref()
        .context("original candidate results are unavailable")?;
    if stored_reference != rt
        || stored_candidate != ct
        || divergence != f64::from(r.output_fingerprint != c.output_fingerprint)
        || time < reference.observed_at_us
        || time < candidate.observed_at_us
    {
        bail!("verified-USD pair differs from its original results");
    }
    Ok(Some(time))
}

/// Pair both economic views in one transaction. Unbound legacy executions keep
/// their legacy contract; a partially bound pair cannot bypass original custody.
pub(crate) fn compare_usd_in_transaction(
    conn: &Connection,
    reference: &str,
    candidate: &str,
    at: i64,
) -> Result<Option<Pair>> {
    let lookup = |token: &str| -> Result<Option<String>> {
        Ok(conn
            .query_row(
                "SELECT execution_id FROM provider_original_usd_comparison WHERE token=?1",
                [token],
                |r| r.get(0),
            )
            .optional()?)
    };
    match (lookup(reference)?, lookup(candidate)?) {
        (Some(reference), Some(candidate)) => Ok(Some(compare_in_transaction(
            conn, &reference, &candidate, at,
        )?)),
        (None, None) => Ok(None),
        _ => bail!("both verified-USD comparisons require original execution bindings"),
    }
}
