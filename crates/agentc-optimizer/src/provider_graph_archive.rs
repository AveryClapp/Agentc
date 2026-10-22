//! Verified immutable role evidence for one operation in an active transaction.
//! Callers may insert evidence or update controller projections, but must not
//! mutate original records or retain this archive across transaction boundaries.
//! Heads, quality events, policies and billing projections are never cached.

use std::collections::{HashMap, HashSet};

use agentc_core::storage::content_hash;
use anyhow::{bail, Context, Result};
use rusqlite::Connection;

use crate::provider_graph_comparison::Pair;
use crate::provider_graph_preparation::{self as preparation, Lease};
use crate::provider_graph_role_comparison as comparison;
use crate::provider_graph_roles::{self as roles, Execution, Prepared};

pub(crate) struct Archive<'a> {
    conn: &'a Connection,
    preparations: HashMap<String, (Lease, Prepared)>,
    executions: HashMap<String, Execution>,
    pairs: HashMap<String, Pair>,
    visiting: HashSet<(&'static str, String)>,
}

impl<'a> Archive<'a> {
    pub(crate) fn new(conn: &'a Connection) -> Result<Self> {
        if conn.is_autocommit() {
            bail!("role archive requires an active transaction");
        }
        Ok(Self {
            conn,
            preparations: HashMap::new(),
            executions: HashMap::new(),
            pairs: HashMap::new(),
            visiting: HashSet::new(),
        })
    }

    fn enter(&mut self, kind: &'static str, id: &str) -> Result<(&'static str, String)> {
        let key = (kind, id.to_owned());
        if !self.visiting.insert(key.clone()) {
            bail!("role evidence ancestry contains a cycle");
        }
        Ok(key)
    }

    pub(crate) fn preparation(&mut self, id: &str) -> Result<(Lease, Prepared)> {
        if let Some(value) = self.preparations.get(id) {
            return Ok(value.clone());
        }
        let visiting = self.enter("preparation", id)?;
        let result: Result<(Lease, Prepared)> = (|| {
            let lease = preparation::load_metadata(self.conn, id)?;
            let prepared = roles::original(self.conn, &lease)?
                .context("completed physical graph program is missing")?;
            if let Some(context) = &lease.role_comparison_context {
                let pair = self.pair(&context.source_pair_id)?;
                comparison::validate_context_custody(&lease, &prepared, &pair)?;
            }
            Ok((lease, prepared))
        })();
        self.visiting.remove(&visiting);
        let value: (Lease, Prepared) = result?;
        self.preparations.insert(id.into(), value.clone());
        Ok(value)
    }

    pub(crate) fn execution(&mut self, id: &str) -> Result<Execution> {
        if let Some(value) = self.executions.get(id) {
            return Ok(value.clone());
        }
        let visiting = self.enter("execution", id)?;
        let result: Result<Execution> = (|| {
            let (hash, raw): (String, String) = self.conn.query_row(
                "SELECT execution_sha256,execution_json FROM provider_graph_role_execution WHERE execution_id=?1",
                [id], |r| Ok((r.get(0)?, r.get(1)?)),
            )?;
            let execution: Execution = serde_json::from_str(&raw)?;
            let (lease, prepared) = self.preparation(&execution.lease_id)?;
            roles::validate_execution(self.conn, id, &hash, &raw, &execution, &lease, &prepared)?;
            Ok(execution)
        })();
        self.visiting.remove(&visiting);
        let value: Execution = result?;
        self.executions.insert(id.into(), value.clone());
        Ok(value)
    }

    pub(crate) fn pair(&mut self, id: &str) -> Result<Pair> {
        if let Some(value) = self.pairs.get(id) {
            return Ok(value.clone());
        }
        let visiting = self.enter("pair", id)?;
        let result = (|| {
            let (hash, raw): (String, String) = self.conn.query_row(
                "SELECT pair_sha256,pair_json FROM provider_graph_role_pair WHERE pair_id=?1",
                [id],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )?;
            let pair: Pair = serde_json::from_str(&raw)?;
            let r = self.execution(&pair.reference_execution_id)?;
            let c = self.execution(&pair.candidate_execution_id)?;
            let (_, rp) = self.preparation(&r.lease_id)?;
            let (_, cp) = self.preparation(&c.lease_id)?;
            comparison::compatible(&r, &c, &rp, &cp)?;
            if content_hash(&raw) != hash
                || pair.pair_id != id
                || pair != comparison::expected(&r, &c, pair.observed_at_us)?
            {
                bail!("role graph pair differs from its immutable executions");
            }
            Ok(pair)
        })();
        self.visiting.remove(&visiting);
        let value: Pair = result?;
        self.pairs.insert(id.into(), value.clone());
        Ok(value)
    }
}
