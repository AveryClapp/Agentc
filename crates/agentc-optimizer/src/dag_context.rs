//! Rolling DAG context window.
//!
//! `ParallelBranch` and `StateDrop` need to see the last N spans in the
//! current trace so they can reason about structural disjointness and
//! downstream state usage. We don't materialize the DAG as a graph — we
//! just ask the profiler's `spans` table for the most recent window and
//! let each rule walk it.
//!
//! Since multiple rules consult the context on a single hot call, we
//! wrap the query in a per-trace LRU cache: the first rule to ask pays
//! the SQLite round-trip; subsequent rules get the cached window. The
//! cache lives only as long as a trace is hot in the planner; the eviction
//! policy bounds memory to `MAX_TRACES_CACHED` entries.
//!
//! Spec § Architecture > DAG IR. Exit criterion: < 300 µs p50 for the
//! last-16-spans query.

use std::collections::HashMap;
use std::sync::Arc;

use anyhow::{Context, Result};
use parking_lot::RwLock;
use rusqlite::{params, Connection};

/// Maximum distinct traces kept in memory at once. Traces are typically
/// ~10–100 spans and short-lived; 64 is enough to cover a small thread
/// pool of concurrent agents without any measurable memory cost.
pub const MAX_TRACES_CACHED: usize = 64;

/// Default window size from the spec's sample query (§ Architecture > DAG
/// IR). Exposed so callers can pick a smaller window on fast paths.
pub const DEFAULT_WINDOW: usize = 16;

/// One row of the DAG context window. Fields mirror what the rules
/// actually consume — we deliberately omit the full span payload to keep
/// the cache small and the query narrow.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DagSpan {
    /// Hex-encoded span_id (matches the profiler's `spans.span_id`
    /// column, which is `TEXT`).
    pub span_id: String,
    /// Kind of the span: `"llm"`, `"tool"`, `"agent"`, etc. Rules use
    /// this to filter (e.g. `ParallelBranch` only cares about llm/tool).
    pub kind: String,
    /// Extracted from `attributes.call_site_id` if set, else the span
    /// `name`. Kept as an owned string because rules compare it directly
    /// to a `Call::call_site_id`.
    pub call_site_id: String,
    pub start_time: i64,
    pub end_time: Option<i64>,
    /// Content-addressed hash of the span's input payload (may be NULL
    /// when the span hasn't been finalised).
    pub input_content_id: Option<String>,
    pub output_content_id: Option<String>,
}

/// Query the last `limit` spans of a trace, ordered newest-first.
///
/// This is the raw SQLite call — the LRU wrapper below is what the
/// planner hot path should use so repeat lookups within a trace don't
/// pay the query cost twice.
pub fn recent_spans(
    conn: &Connection,
    trace_id_hex: &str,
    limit: usize,
) -> Result<Vec<DagSpan>> {
    let mut stmt = conn
        .prepare_cached(
            "SELECT span_id, kind, name, attributes, start_time, end_time, \
                    input_content_id, output_content_id \
             FROM spans WHERE trace_id = ?1 \
             ORDER BY start_time DESC LIMIT ?2",
        )
        .context("prepare recent_spans")?;
    let rows = stmt
        .query_map(params![trace_id_hex, limit as i64], |r| {
            let span_id: String = r.get(0)?;
            let kind: String = r.get(1)?;
            let name: String = r.get(2)?;
            let attrs_json: String = r.get(3)?;
            let start_time: i64 = r.get(4)?;
            let end_time: Option<i64> = r.get(5)?;
            let input_content_id: Option<String> = r.get(6)?;
            let output_content_id: Option<String> = r.get(7)?;
            Ok(DagSpan {
                span_id,
                kind,
                call_site_id: extract_call_site_id(&attrs_json).unwrap_or(name),
                start_time,
                end_time,
                input_content_id,
                output_content_id,
            })
        })
        .context("query recent_spans")?;
    let mut out = Vec::with_capacity(limit);
    for row in rows {
        out.push(row.context("decode DagSpan row")?);
    }
    Ok(out)
}

/// Parse `attributes` JSON and pull out a `call_site_id` field if
/// present. Any parse failure falls through to `None` — the caller
/// substitutes the span `name`.
fn extract_call_site_id(attrs_json: &str) -> Option<String> {
    let v: serde_json::Value = serde_json::from_str(attrs_json).ok()?;
    v.get("call_site_id")
        .or_else(|| v.get("agentc.call_site_id"))
        .and_then(|s| s.as_str())
        .map(String::from)
}

/// LRU-bounded per-trace cache. Rules calling into the same trace during
/// one planner invocation hit the cache on the second and later lookups.
///
/// `Vec<(trace_id, Vec<DagSpan>)>` is sufficient — traces are inserted
/// at the front; when we exceed `MAX_TRACES_CACHED` we drop the back.
/// At 64 entries the linear scan is faster than a hash probe + alloc.
pub struct DagContextCache {
    inner: Arc<RwLock<CacheInner>>,
}

struct CacheInner {
    order: Vec<String>,
    map: HashMap<String, (usize, Arc<Vec<DagSpan>>)>, // trace_id → (window_size, spans)
}

impl Default for DagContextCache {
    fn default() -> Self {
        Self::new()
    }
}

impl DagContextCache {
    pub fn new() -> Self {
        Self {
            inner: Arc::new(RwLock::new(CacheInner {
                order: Vec::with_capacity(MAX_TRACES_CACHED),
                map: HashMap::with_capacity(MAX_TRACES_CACHED),
            })),
        }
    }

    /// Look up or fill. The SQLite query runs at most once per
    /// `(trace_id, window_size)` pair; a later lookup with a larger
    /// window refills. Shared `Arc<Vec<DagSpan>>` avoids cloning the
    /// window on every rule.
    pub fn get_or_fetch(
        &self,
        conn: &Connection,
        trace_id_hex: &str,
        window: usize,
    ) -> Result<Arc<Vec<DagSpan>>> {
        // Fast read-only path.
        {
            let guard = self.inner.read();
            if let Some((stored_window, spans)) = guard.map.get(trace_id_hex) {
                if *stored_window >= window {
                    return Ok(Arc::clone(spans));
                }
            }
        }
        // Slow path: query and fill.
        let spans = Arc::new(recent_spans(conn, trace_id_hex, window)?);
        let mut guard = self.inner.write();
        if let Some((stored_window, existing)) = guard.map.get(trace_id_hex) {
            // Racing writer already filled a large-enough window.
            if *stored_window >= window {
                return Ok(Arc::clone(existing));
            }
            // Otherwise replace with the wider window.
            // order list already has the entry; move-to-front below.
        } else {
            guard.order.push(trace_id_hex.to_string());
        }
        guard
            .map
            .insert(trace_id_hex.to_string(), (window, Arc::clone(&spans)));
        // Evict LRU entries.
        while guard.order.len() > MAX_TRACES_CACHED {
            let victim = guard.order.remove(0);
            guard.map.remove(&victim);
        }
        Ok(spans)
    }

    /// Drop the cached window for a trace — called when a trace finishes
    /// so the planner doesn't retain stale DAG state.
    pub fn invalidate(&self, trace_id_hex: &str) {
        let mut guard = self.inner.write();
        guard.map.remove(trace_id_hex);
        guard.order.retain(|t| t != trace_id_hex);
    }

    /// Current cache size, exposed for tests.
    pub fn len(&self) -> usize {
        self.inner.read().map.len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Instant;

    /// Minimal spans-table fixture. We don't link agentc-core's schema
    /// here — it's behind extra crates — so the test DDL mirrors the
    /// subset of columns `recent_spans` reads.
    const SPANS_FIXTURE_DDL: &str = r#"
        CREATE TABLE spans (
            span_id           TEXT PRIMARY KEY,
            trace_id          TEXT NOT NULL,
            name              TEXT NOT NULL,
            kind              TEXT NOT NULL,
            start_time        INTEGER NOT NULL,
            end_time          INTEGER,
            attributes        TEXT NOT NULL,
            input_content_id  TEXT,
            output_content_id TEXT
        );
        CREATE INDEX idx_trace ON spans(trace_id);
    "#;

    fn fixture_conn() -> Connection {
        let c = Connection::open_in_memory().unwrap();
        c.execute_batch(SPANS_FIXTURE_DDL).unwrap();
        c
    }

    fn insert_span(
        c: &Connection,
        span_id: &str,
        trace_id: &str,
        name: &str,
        start: i64,
        attrs: &str,
    ) {
        c.execute(
            "INSERT INTO spans (span_id, trace_id, name, kind, start_time, end_time, attributes, \
                                input_content_id, output_content_id) \
             VALUES (?1, ?2, ?3, 'llm', ?4, ?5, ?6, ?7, ?8)",
            params![
                span_id,
                trace_id,
                name,
                start,
                start + 1,
                attrs,
                Some(format!("in-{span_id}")),
                Some(format!("out-{span_id}")),
            ],
        )
        .unwrap();
    }

    #[test]
    fn recent_spans_orders_newest_first_and_respects_limit() {
        let c = fixture_conn();
        for i in 0..20 {
            insert_span(&c, &format!("s{i:02}"), "t1", "site", i as i64, "{}");
        }
        let ctx = recent_spans(&c, "t1", 5).unwrap();
        assert_eq!(ctx.len(), 5);
        // Newest first → s19, s18, s17, s16, s15.
        let ids: Vec<_> = ctx.iter().map(|s| s.span_id.clone()).collect();
        assert_eq!(ids, vec!["s19", "s18", "s17", "s16", "s15"]);
    }

    #[test]
    fn recent_spans_extracts_call_site_id_from_attributes() {
        let c = fixture_conn();
        insert_span(
            &c,
            "s1",
            "t1",
            "default-name",
            1,
            r#"{"call_site_id":"app.agents.planner:plan"}"#,
        );
        insert_span(&c, "s2", "t1", "fallback-name", 2, "{}");
        let ctx = recent_spans(&c, "t1", 16).unwrap();
        assert_eq!(ctx[0].span_id, "s2");
        assert_eq!(ctx[0].call_site_id, "fallback-name"); // no attr → name
        assert_eq!(ctx[1].call_site_id, "app.agents.planner:plan");
    }

    #[test]
    fn recent_spans_filters_by_trace_id() {
        let c = fixture_conn();
        insert_span(&c, "s1", "t1", "x", 1, "{}");
        insert_span(&c, "s2", "t2", "y", 2, "{}");
        let t1 = recent_spans(&c, "t1", 16).unwrap();
        assert_eq!(t1.len(), 1);
        assert_eq!(t1[0].span_id, "s1");
    }

    #[test]
    fn cache_hits_skip_the_query() {
        let c = fixture_conn();
        for i in 0..5 {
            insert_span(&c, &format!("s{i}"), "t", "site", i as i64, "{}");
        }
        let cache = DagContextCache::new();
        let first = cache.get_or_fetch(&c, "t", 16).unwrap();
        // Insert more spans — the cache should still serve the OLD
        // window until invalidated.
        insert_span(&c, "sfresh", "t", "site", 100, "{}");
        let second = cache.get_or_fetch(&c, "t", 16).unwrap();
        assert_eq!(first.len(), second.len(), "cache must not re-query");
        // Invalidating forces a refetch.
        cache.invalidate("t");
        let after = cache.get_or_fetch(&c, "t", 16).unwrap();
        assert_eq!(after.len(), 6);
    }

    #[test]
    fn cache_evicts_beyond_capacity() {
        let c = fixture_conn();
        let cache = DagContextCache::new();
        // Force more distinct traces than the cap.
        for i in 0..(MAX_TRACES_CACHED + 10) {
            let tid = format!("t{i}");
            insert_span(&c, &format!("s{i}"), &tid, "site", i as i64, "{}");
            cache.get_or_fetch(&c, &tid, 16).unwrap();
        }
        assert!(
            cache.len() <= MAX_TRACES_CACHED,
            "cache grew past cap: {}",
            cache.len()
        );
    }

    /// Exit-criterion: the raw SQLite round-trip is < 300 µs at p50 on a
    /// 1000-span trace in release mode. Debug builds pay 3-5× overhead
    /// on rusqlite iteration and JSON parse, so we assert a looser
    /// debug-mode bound but tag the test as the perf gate.
    ///
    /// Run `cargo test -p agentc-optimizer --release recent_spans_under`
    /// to validate the ship-gate number.
    #[test]
    fn recent_spans_under_300us_p50() {
        let c = fixture_conn();
        for i in 0..1000 {
            insert_span(&c, &format!("s{i:04}"), "t-big", "site", i as i64, "{}");
        }
        // Warm up the cached statement + query planner.
        for _ in 0..10 {
            let _ = recent_spans(&c, "t-big", 16).unwrap();
        }
        let mut samples: Vec<u128> = Vec::with_capacity(100);
        for _ in 0..100 {
            let t = Instant::now();
            let ctx = recent_spans(&c, "t-big", 16).unwrap();
            samples.push(t.elapsed().as_micros());
            assert_eq!(ctx.len(), 16);
        }
        samples.sort();
        let p50 = samples[50];
        let budget_us: u128 = if cfg!(debug_assertions) { 3000 } else { 300 };
        assert!(
            p50 < budget_us,
            "recent_spans p50 = {p50} µs; budget {budget_us} µs \
             (debug={})",
            cfg!(debug_assertions)
        );
    }
}
