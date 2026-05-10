//! `plan_audit` writer.
//!
//! The audit table is a fixed-capacity ring buffer: every plan dispatch
//! INSERTs one row; once the count passes [`RING_BUFFER_CAP`] the writer
//! thread prunes the oldest `excess` rows. AUTOINCREMENT guarantees pruned
//! IDs are never reused, so `agentc optimize inspect <audit_id>` is
//! unambiguous for the lifetime of the DB.
//!
//! The prune is a single `DELETE … WHERE audit_id < ?` with a secondary
//! index on `audit_id` (primary key). The spec requires < 100 ms per prune
//! at the 10,000-row threshold — exercised by the `prune_under_100ms` test.

use anyhow::{Context, Result};
use rusqlite::{params, Connection, Transaction};

/// Capacity of the audit ring buffer before we start pruning. Exposed as a
/// `pub const` so downstream tests can reference it when they fill to the
/// boundary.
pub const RING_BUFFER_CAP: i64 = 10_000;

/// Discriminator column for `plan_audit.plan_kind`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PlanKind {
    PassThrough,
    Cached,
    Rewritten,
    Parallel,
    Composed,
}

impl PlanKind {
    pub fn as_str(&self) -> &'static str {
        match self {
            PlanKind::PassThrough => "pass_through",
            PlanKind::Cached => "cached",
            PlanKind::Rewritten => "rewritten",
            PlanKind::Parallel => "parallel",
            PlanKind::Composed => "composed",
        }
    }
}

/// Value type for one `plan_audit` row.
#[derive(Debug, Clone)]
pub struct PlanAudit {
    pub ts_us: i64,
    pub call_site_id: String,
    pub span_id: [u8; 8],
    pub plan_kind: PlanKind,
    pub rule: Option<String>,
    pub projected_savings_usd: Option<f64>,
    pub measured_savings_usd: Option<f64>,
    pub overhead_us: i64,
    pub shadow_sampled: bool,
    pub shadow_divergence: Option<f64>,
}

/// Append one audit row. Does **not** prune — the writer thread batches the
/// prune into a separate step so hot inserts never pay for maintenance.
pub fn insert(conn: &Connection, audit: &PlanAudit) -> Result<i64> {
    conn.execute(
        "INSERT INTO plan_audit (\
            ts_us, call_site_id, span_id, plan_kind, rule, \
            projected_savings_usd, measured_savings_usd, \
            overhead_us, shadow_sampled, shadow_divergence\
         ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10)",
        params![
            audit.ts_us,
            audit.call_site_id,
            audit.span_id.to_vec(),
            audit.plan_kind.as_str(),
            audit.rule,
            audit.projected_savings_usd,
            audit.measured_savings_usd,
            audit.overhead_us,
            audit.shadow_sampled as i64,
            audit.shadow_divergence,
        ],
    )
    .context("insert plan_audit")?;
    Ok(conn.last_insert_rowid())
}

/// Batched insert. Cheaper than N single inserts because the whole batch
/// shares one transaction.
pub fn insert_batch(conn: &mut Connection, rows: &[PlanAudit]) -> Result<usize> {
    if rows.is_empty() {
        return Ok(0);
    }
    let tx = conn.transaction().context("begin plan_audit batch")?;
    {
        let mut stmt = tx.prepare(
            "INSERT INTO plan_audit (\
                ts_us, call_site_id, span_id, plan_kind, rule, \
                projected_savings_usd, measured_savings_usd, \
                overhead_us, shadow_sampled, shadow_divergence\
             ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10)",
        )?;
        for r in rows {
            stmt.execute(params![
                r.ts_us,
                r.call_site_id,
                r.span_id.to_vec(),
                r.plan_kind.as_str(),
                r.rule,
                r.projected_savings_usd,
                r.measured_savings_usd,
                r.overhead_us,
                r.shadow_sampled as i64,
                r.shadow_divergence,
            ])?;
        }
    }
    tx.commit().context("commit plan_audit batch")?;
    Ok(rows.len())
}

/// Prune any rows beyond `cap` by deleting the oldest `audit_id`s. Runs on
/// the writer thread; safe to call every Nth insert.
///
/// Returns the number of rows deleted.
pub fn prune(conn: &mut Connection, cap: i64) -> Result<u64> {
    let count: i64 = conn
        .query_row("SELECT COUNT(*) FROM plan_audit", [], |r| r.get(0))
        .context("count plan_audit")?;
    if count <= cap {
        return Ok(0);
    }
    let excess = count - cap;
    // Pick the `excess`-th smallest audit_id — we delete strictly below it.
    let cutoff: Option<i64> = conn
        .query_row(
            "SELECT audit_id FROM plan_audit ORDER BY audit_id ASC LIMIT 1 OFFSET ?1",
            params![excess],
            |r| r.get(0),
        )
        .ok();
    let Some(cutoff_id) = cutoff else {
        return Ok(0);
    };
    let tx = conn.transaction().context("begin prune")?;
    prune_under(&tx, cutoff_id)?;
    tx.commit().context("commit prune")?;
    Ok(excess as u64)
}

fn prune_under(tx: &Transaction<'_>, cutoff_id: i64) -> Result<()> {
    tx.execute("DELETE FROM plan_audit WHERE audit_id < ?1", params![cutoff_id])
        .context("delete rows below cutoff")?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::schema::ensure_audit_schema;
    use std::time::Instant;

    fn fresh_conn() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        ensure_audit_schema(&conn).unwrap();
        conn
    }

    fn sample_row(i: i64) -> PlanAudit {
        PlanAudit {
            ts_us: i,
            call_site_id: format!("site-{}", i % 4),
            span_id: [0u8; 8],
            plan_kind: PlanKind::PassThrough,
            rule: None,
            projected_savings_usd: None,
            measured_savings_usd: None,
            overhead_us: 42,
            shadow_sampled: false,
            shadow_divergence: None,
        }
    }

    #[test]
    fn insert_single_row_returns_audit_id() {
        let conn = fresh_conn();
        let id = insert(&conn, &sample_row(1)).unwrap();
        assert_eq!(id, 1);
    }

    #[test]
    fn insert_batch_writes_every_row() {
        let mut conn = fresh_conn();
        let rows: Vec<PlanAudit> = (0..25).map(sample_row).collect();
        let n = insert_batch(&mut conn, &rows).unwrap();
        assert_eq!(n, 25);
        let count: i64 = conn
            .query_row("SELECT COUNT(*) FROM plan_audit", [], |r| r.get(0))
            .unwrap();
        assert_eq!(count, 25);
    }

    #[test]
    fn prune_is_a_noop_below_cap() {
        let mut conn = fresh_conn();
        let rows: Vec<PlanAudit> = (0..10).map(sample_row).collect();
        insert_batch(&mut conn, &rows).unwrap();
        let removed = prune(&mut conn, 100).unwrap();
        assert_eq!(removed, 0);
    }

    #[test]
    fn prune_drops_oldest_excess_rows() {
        let mut conn = fresh_conn();
        let rows: Vec<PlanAudit> = (0..20).map(sample_row).collect();
        insert_batch(&mut conn, &rows).unwrap();
        let removed = prune(&mut conn, 15).unwrap();
        assert_eq!(removed, 5);
        let remaining: i64 = conn
            .query_row("SELECT COUNT(*) FROM plan_audit", [], |r| r.get(0))
            .unwrap();
        assert_eq!(remaining, 15);
        // The oldest 5 IDs (1..=5) were deleted; 6..=20 survive.
        let min_id: i64 = conn
            .query_row("SELECT MIN(audit_id) FROM plan_audit", [], |r| r.get(0))
            .unwrap();
        assert_eq!(min_id, 6);
    }

    /// Exit-criteria: prune at the 10,000-row threshold completes in < 100 ms.
    /// We load 12,000 rows, measure, and assert.
    #[test]
    fn prune_under_100ms_at_ring_buffer_cap() {
        let mut conn = fresh_conn();
        let rows: Vec<PlanAudit> = (0..12_000).map(sample_row).collect();
        insert_batch(&mut conn, &rows).unwrap();
        let started = Instant::now();
        let removed = prune(&mut conn, RING_BUFFER_CAP).unwrap();
        let elapsed = started.elapsed();
        assert_eq!(removed, 2_000);
        assert!(
            elapsed.as_millis() < 100,
            "prune took {elapsed:?}; spec caps this at 100ms"
        );
    }
}
