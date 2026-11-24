//! PyO3 bindings for the Agentc profiler.
//!
//! Exposes `_native` Python module with `write_span()` as the primary FFI entry point.
//! All heavy lifting (hashing, compression, embedding, SQLite writes) happens on the Rust
//! side. The Python layer is as thin as possible.

#![allow(clippy::useless_conversion)] // PyO3 macro-generated code triggers this

use std::panic::AssertUnwindSafe;
use std::path::Path;
use std::sync::{Arc, Mutex, OnceLock, RwLock};

use pyo3::exceptions::{PyRuntimeError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList};
use rusqlite::Connection;

use agentc_core::storage::{self, SpanInput, WriteSpanOptions};
use agentc_memo::key::InvalidationPattern;
use agentc_optimizer::{
    audit::{insert as audit_insert, PlanAudit, PlanKind},
    build_optimizer,
    config::OptimizerConfig,
    cost_model::CostModel,
    ffi::{
        cancel_embedded_exploration as rust_cancel_embedded_exploration,
        complete_profiled_exploration as rust_complete_exploration,
        fail_profiled_exploration as rust_fail_exploration, guard_and_attach_observation_context,
        optimize_frozen_policy as rust_frozen_policy, optimize_observe as rust_observe,
        optimize_plan as rust_projected_plan,
        optimize_profiled_plan_with_audit as rust_profiled_plan, record_observation_divergence,
        reserve_profiled_exploration as rust_reserve_exploration, unavailable_selection_audit,
        DivergenceAttribution, PASS_THROUGH_JSON,
    },
    model_catalog::{default_model_catalog, ModelCatalog},
    plan_guard::{PlanGuard, PlanGuardOutcome},
    plan_profile::PlanProfiles,
    planner::{Optimizer, Plan},
    Budget, ExplorationController, SampleOutcome, Wired,
};

/// Package version, exposed as `agentc._native.__version__`.
const VERSION: &str = env!("CARGO_PKG_VERSION");

/// Required keys that must be present in every span dict.
const REQUIRED_KEYS: &[&str] = &["span_id", "trace_id", "name", "kind", "start_time"];

/// Process-global state: per-process DB connection + write options.
///
/// Initialized by `create_db()`; consumed by `write_span()`.
struct ProfilerState {
    conn: Option<Connection>,
    opts: WriteSpanOptions,
}

static STATE: OnceLock<Mutex<ProfilerState>> = OnceLock::new();

fn state() -> &'static Mutex<ProfilerState> {
    STATE.get_or_init(|| {
        Mutex::new(ProfilerState {
            conn: None,
            opts: WriteSpanOptions::default(),
        })
    })
}

/// Validate that the span dict contains all required keys.
fn validate_span_dict(span_dict: &Bound<'_, PyDict>) -> PyResult<()> {
    for &key in REQUIRED_KEYS {
        if !span_dict.contains(key)? {
            return Err(PyValueError::new_err(format!(
                "write_span: missing required key '{key}'"
            )));
        }
    }
    Ok(())
}

/// Extract an optional string field from a span dict.
fn opt_str(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<Option<String>> {
    match dict.get_item(key)? {
        Some(v) if !v.is_none() => Ok(Some(v.extract::<String>()?)),
        _ => Ok(None),
    }
}

/// Extract an optional i64 field from a span dict.
fn opt_i64(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<Option<i64>> {
    match dict.get_item(key)? {
        Some(v) if !v.is_none() => Ok(Some(v.extract::<i64>()?)),
        _ => Ok(None),
    }
}

/// Parse a string field from `attributes` JSON (for promoted columns).
fn attrs_str(attrs: &serde_json::Value, key: &str) -> Option<String> {
    attrs
        .get(key)
        .and_then(|v| v.as_str())
        .map(|s| s.to_string())
}

/// Parse an i64 field from `attributes` JSON (for promoted token columns).
fn attrs_i64(attrs: &serde_json::Value, key: &str) -> Option<i64> {
    attrs.get(key).and_then(|v| v.as_i64())
}

/// Parse a JSON-string field (e.g. input_messages) from the dict.
fn parse_json_str_field(
    dict: &Bound<'_, PyDict>,
    key: &str,
) -> PyResult<Option<serde_json::Value>> {
    match dict.get_item(key)? {
        Some(v) if !v.is_none() => {
            let s = v.extract::<String>()?;
            let parsed: serde_json::Value = serde_json::from_str(&s).map_err(|e| {
                PyValueError::new_err(format!("write_span: {key} is not valid JSON: {e}"))
            })?;
            Ok(Some(parsed))
        }
        _ => Ok(None),
    }
}

/// Build a `SpanInput` from a validated span dict.
///
/// Top-level dict keys take precedence; promoted columns (model, provider, tokens)
/// fall back to parsing the `attributes` JSON blob under `gen_ai.*` keys.
fn build_span_input(dict: &Bound<'_, PyDict>) -> PyResult<SpanInput> {
    let span_id: String = dict.get_item("span_id")?.unwrap().extract()?;
    let trace_id: String = dict.get_item("trace_id")?.unwrap().extract()?;
    let name: String = dict.get_item("name")?.unwrap().extract()?;
    let kind: String = dict.get_item("kind")?.unwrap().extract()?;
    let start_time: i64 = dict.get_item("start_time")?.unwrap().extract()?;

    let parent_span_id = opt_str(dict, "parent_span_id")?;
    let end_time = opt_i64(dict, "end_time")?;
    let status = opt_str(dict, "status")?.unwrap_or_else(|| "OK".to_string());

    let attributes_json = opt_str(dict, "attributes")?.unwrap_or_else(|| "{}".to_string());
    let attrs_parsed: serde_json::Value = serde_json::from_str(&attributes_json)
        .unwrap_or(serde_json::Value::Object(Default::default()));

    let model = opt_str(dict, "model")?
        .or_else(|| attrs_str(&attrs_parsed, "gen_ai.response.model"))
        .or_else(|| attrs_str(&attrs_parsed, "gen_ai.request.model"));
    let provider =
        opt_str(dict, "provider")?.or_else(|| attrs_str(&attrs_parsed, "gen_ai.provider.name"));
    let input_tokens = opt_i64(dict, "input_tokens")?
        .or_else(|| attrs_i64(&attrs_parsed, "gen_ai.usage.input_tokens"));
    let output_tokens = opt_i64(dict, "output_tokens")?
        .or_else(|| attrs_i64(&attrs_parsed, "gen_ai.usage.output_tokens"));
    let cache_creation_tokens = opt_i64(dict, "cache_creation_tokens")?
        .or_else(|| attrs_i64(&attrs_parsed, "gen_ai.usage.cache_creation.input_tokens"));
    let cache_read_tokens = opt_i64(dict, "cache_read_tokens")?
        .or_else(|| attrs_i64(&attrs_parsed, "gen_ai.usage.cache_read.input_tokens"));

    let input_messages = parse_json_str_field(dict, "input_messages")?;
    let output_messages = parse_json_str_field(dict, "output_messages")?;

    Ok(SpanInput {
        span_id,
        trace_id,
        parent_span_id,
        name,
        kind,
        start_time,
        end_time,
        status,
        model,
        provider,
        input_tokens,
        output_tokens,
        cache_creation_tokens,
        cache_read_tokens,
        attributes: attributes_json,
        input_messages,
        output_messages,
    })
}

/// Write a span dict from Python into the native storage layer.
///
/// The dict must contain at minimum: `span_id`, `trace_id`, `name`, `kind`, `start_time`.
/// Optional keys: `parent_span_id`, `end_time`, `status`, `model`, `provider`,
/// `input_tokens`, `output_tokens`, `cache_creation_tokens`, `cache_read_tokens`,
/// `attributes` (JSON string), `input_messages` (JSON string), `output_messages`
/// (JSON string).
///
/// If `create_db()` has not been called, the span is silently dropped (fail-open).
#[pyfunction]
fn write_span(py: Python<'_>, span_dict: &Bound<'_, PyAny>) -> PyResult<()> {
    let dict = span_dict
        .downcast::<PyDict>()
        .map_err(|_| PyTypeError::new_err("write_span: expected a dict argument"))?;
    validate_span_dict(dict)?;

    let input = build_span_input(dict)?;

    py.allow_threads(|| -> PyResult<()> {
        let mut guard = state().lock().map_err(|e| {
            PyRuntimeError::new_err(format!("write_span: state lock poisoned: {e}"))
        })?;
        let opts = guard.opts;
        let Some(ref conn) = guard.conn else {
            // No DB configured — fail-open, silently drop.
            return Ok(());
        };
        storage::write_span(conn, &input, opts)
            .map_err(|e| PyRuntimeError::new_err(format!("write_span: {e}")))?;
        // Silence unused-mut warning when the borrow is short-lived.
        let _ = &mut guard;
        Ok(())
    })
}

/// Create or open a SQLite database at the given path.
///
/// If called multiple times in one process, the most recent connection replaces
/// the previous one (e.g. re-init after shutdown).
#[pyfunction]
#[pyo3(signature = (path, is_canonical, capture_content=true, capture_embeddings=true))]
fn create_db(
    path: &str,
    is_canonical: bool,
    capture_content: bool,
    capture_embeddings: bool,
) -> PyResult<()> {
    let conn = agentc_core::db::create_db(Path::new(path), is_canonical)
        .map_err(|e| PyRuntimeError::new_err(format!("create_db: {e}")))?;

    // Apply the memoization DDL on the same connection so @memoize can
    // share the profiler's per-process DB without a separate bootstrap.
    agentc_memo::ensure_schema(&conn)
        .map_err(|e| PyRuntimeError::new_err(format!("create_db: memoization schema: {e}")))?;

    let mut guard = state()
        .lock()
        .map_err(|e| PyRuntimeError::new_err(format!("create_db: state lock poisoned: {e}")))?;
    guard.conn = Some(conn);
    guard.opts = WriteSpanOptions {
        capture_content,
        capture_embeddings: capture_content && capture_embeddings,
    };
    Ok(())
}

/// Query all spans for a given trace_id from a SQLite database.
///
/// Returns a list of dicts, each representing a span. If the DB does not exist,
/// returns an empty list (not an error).
#[pyfunction]
fn query_spans_by_trace(py: Python<'_>, db_path: &str, trace_id: &str) -> PyResult<Py<PyList>> {
    let path = Path::new(db_path);
    if !path.exists() {
        return Ok(PyList::empty_bound(py).unbind());
    }

    let spans = py.allow_threads(|| -> PyResult<Vec<agentc_core::span::Span>> {
        let conn = agentc_core::db::open_db(path)
            .map_err(|e| PyRuntimeError::new_err(format!("query_spans_by_trace: {e}")))?;
        agentc_core::db::query_spans_by_trace(&conn, trace_id)
            .map_err(|e| PyRuntimeError::new_err(format!("query_spans_by_trace: {e}")))
    })?;

    let list = PyList::empty_bound(py);
    for span in &spans {
        let d = span_to_pydict(py, span)?;
        list.append(d)?;
    }
    Ok(list.unbind())
}

/// Read prior-span content for a trace from the per-process active DB.
///
/// Returns a list of dicts with keys `span_id`, `trace_id`, `parent_span_id`,
/// `start_time`, `input_messages` (decompressed JSON string or `None`),
/// `output_messages` (decompressed JSON string or `None`). Used by the
/// attention proxy in `agentc._attention` to build a multi-turn salient
/// signal without re-tokenizing every prior call from scratch.
///
/// Fail-open: if the per-process DB isn't open, returns an empty list. Per-row
/// decompression failures drop just that field, not the row, so a corrupted
/// content blob doesn't blind us to the rest of the trace.
#[pyfunction]
fn read_trace_content<'py>(py: Python<'py>, trace_id: &str) -> PyResult<Py<PyList>> {
    let trace_id = trace_id.to_string();
    let rows = py.allow_threads(|| -> Vec<TraceContentRow> {
        let Ok(guard) = state().lock() else {
            return Vec::new();
        };
        let Some(conn) = guard.conn.as_ref() else {
            return Vec::new();
        };
        read_trace_content_rows(conn, &trace_id).unwrap_or_default()
    });

    let list = PyList::empty_bound(py);
    for row in &rows {
        let d = PyDict::new_bound(py);
        d.set_item("span_id", &row.span_id)?;
        d.set_item("trace_id", &row.trace_id)?;
        d.set_item("parent_span_id", &row.parent_span_id)?;
        d.set_item("start_time", row.start_time)?;
        d.set_item("input_messages", &row.input_messages)?;
        d.set_item("output_messages", &row.output_messages)?;
        list.append(d)?;
    }
    Ok(list.unbind())
}

struct TraceContentRow {
    span_id: String,
    trace_id: String,
    parent_span_id: Option<String>,
    start_time: i64,
    input_messages: Option<String>,
    output_messages: Option<String>,
}

fn read_trace_content_rows(
    conn: &Connection,
    trace_id: &str,
) -> rusqlite::Result<Vec<TraceContentRow>> {
    let mut stmt = conn.prepare(
        "SELECT s.span_id, s.trace_id, s.parent_span_id, s.start_time, \
                ic.content_text, oc.content_text \
         FROM spans s \
         LEFT JOIN input_content  ic ON s.input_content_id  = ic.content_id \
         LEFT JOIN output_content oc ON s.output_content_id = oc.content_id \
         WHERE s.trace_id = ?1 \
         ORDER BY s.start_time ASC",
    )?;
    let rows: Vec<TraceContentRow> = stmt
        .query_map([trace_id], |row| {
            let span_id: String = row.get(0)?;
            let trace_id: String = row.get(1)?;
            let parent_span_id: Option<String> = row.get(2)?;
            let start_time: i64 = row.get(3)?;
            let input_blob: Option<Vec<u8>> = row.get(4)?;
            let output_blob: Option<Vec<u8>> = row.get(5)?;
            let input_messages = input_blob.and_then(|b| {
                storage::decompress_content(&b)
                    .ok()
                    .and_then(|bytes| String::from_utf8(bytes).ok())
            });
            let output_messages = output_blob.and_then(|b| {
                storage::decompress_content(&b)
                    .ok()
                    .and_then(|bytes| String::from_utf8(bytes).ok())
            });
            Ok(TraceContentRow {
                span_id,
                trace_id,
                parent_span_id,
                start_time,
                input_messages,
                output_messages,
            })
        })?
        .filter_map(|r| r.ok())
        .collect();
    Ok(rows)
}

/// Convert a `Span` into a Python dict (no compression/embedding round-trip; raw fields).
fn span_to_pydict<'py>(
    py: Python<'py>,
    span: &agentc_core::span::Span,
) -> PyResult<Bound<'py, PyDict>> {
    let d = PyDict::new_bound(py);
    d.set_item("span_id", &span.span_id)?;
    d.set_item("trace_id", &span.trace_id)?;
    d.set_item("parent_span_id", &span.parent_span_id)?;
    d.set_item("name", &span.name)?;
    d.set_item("kind", &span.kind)?;
    d.set_item("start_time", span.start_time)?;
    d.set_item("end_time", span.end_time)?;
    d.set_item("status", &span.status)?;
    d.set_item("model", &span.model)?;
    d.set_item("provider", &span.provider)?;
    d.set_item("input_tokens", span.input_tokens)?;
    d.set_item("output_tokens", span.output_tokens)?;
    d.set_item("cache_creation_tokens", span.cache_creation_tokens)?;
    d.set_item("cache_read_tokens", span.cache_read_tokens)?;
    d.set_item("cost_usd", span.cost_usd)?;
    d.set_item("attributes", &span.attributes)?;
    d.set_item("input_content_id", &span.input_content_id)?;
    d.set_item("output_content_id", &span.output_content_id)?;
    d.set_item("embedding_model", &span.embedding_model)?;
    Ok(d)
}

/// Merge all pending per-process DBs into the canonical store.
///
/// Releases the GIL during the merge (which acquires a cross-process flock and
/// does SQLite IO). Returns a dict with merge statistics:
/// `{"spans_merged": int, "input_content_merged": int, "output_content_merged": int}`.
///
/// On non-unix platforms, returns a zeroed stats dict without touching the disk.
#[pyfunction]
fn merge_all_pending(py: Python<'_>) -> PyResult<Py<PyDict>> {
    #[cfg(unix)]
    let stats = py.allow_threads(|| {
        agentc_core::merge::merge_all_pending()
            .map_err(|e| PyRuntimeError::new_err(format!("merge_all_pending: {e}")))
    })?;

    #[cfg(not(unix))]
    let stats = agentc_core::merge::MergeStats::default();

    let d = PyDict::new_bound(py);
    d.set_item("spans_merged", stats.spans_merged)?;
    d.set_item("input_content_merged", stats.input_content_merged)?;
    d.set_item("output_content_merged", stats.output_content_merged)?;
    Ok(d.unbind())
}

/// Look up a memoized response by exact-hash cache key.
///
/// Returns `None` on miss, on any internal error, or when memoization is not
/// initialized. The caller treats `None` as a safe fallback — the LLM call
/// proceeds normally.
#[pyfunction]
#[pyo3(signature = (prompt_hash, model, parameters_hash, call_site_id, embedding=None, similarity=None))]
#[allow(clippy::too_many_arguments)]
fn cache_lookup<'py>(
    py: Python<'py>,
    prompt_hash: &[u8],
    model: &str,
    parameters_hash: &[u8],
    call_site_id: &str,
    embedding: Option<&[u8]>,
    similarity: Option<f32>,
) -> PyResult<Option<Bound<'py, PyDict>>> {
    let embedding_vec = embedding.and_then(decode_embedding_bytes);
    let hit_opt = py.allow_threads(|| -> Option<agentc_memo::CacheHit> {
        let guard = state().lock().ok()?;
        let conn = guard.conn.as_ref()?;
        agentc_memo::ffi::lookup(
            conn,
            prompt_hash,
            model,
            parameters_hash,
            call_site_id,
            embedding_vec.as_deref(),
            similarity,
        )
    });

    let Some(hit) = hit_opt else {
        return Ok(None);
    };

    let d = PyDict::new_bound(py);
    d.set_item("output_content_id", &hit.value.output_content_id)?;
    d.set_item("input_tokens", hit.value.input_tokens)?;
    d.set_item("output_tokens", hit.value.output_tokens)?;
    d.set_item("recorded_cost_usd", hit.value.recorded_cost_usd)?;
    d.set_item("age_micros", hit.age_micros)?;
    d.set_item(
        "source",
        match hit.source {
            agentc_memo::CacheSource::Exact => "exact",
            agentc_memo::CacheSource::Lsh { .. } => "lsh",
        },
    )?;
    if let agentc_memo::CacheSource::Lsh { similarity } = hit.source {
        d.set_item("similarity", similarity)?;
    }
    Ok(Some(d))
}

/// Insert a memoization entry. Enqueued from Python by the writer thread.
///
/// Writes to `output_content` and `memoization_cache` in a single transaction.
/// Fails open: any error is logged (`stderr`) and swallowed; the Python writer
/// loop is already designed to survive FFI failures.
#[pyfunction]
#[pyo3(signature = (prompt_hash, model, parameters_hash, call_site_id, output_bytes, input_tokens, output_tokens, recorded_cost_usd, ttl_seconds, embedding=None))]
#[allow(clippy::too_many_arguments)]
fn cache_insert(
    py: Python<'_>,
    prompt_hash: &[u8],
    model: &str,
    parameters_hash: &[u8],
    call_site_id: &str,
    output_bytes: &[u8],
    input_tokens: u32,
    output_tokens: u32,
    recorded_cost_usd: f32,
    ttl_seconds: i64,
    embedding: Option<&[u8]>,
) -> PyResult<()> {
    let embedding_vec = embedding.and_then(decode_embedding_bytes);
    py.allow_threads(|| -> PyResult<()> {
        let mut guard = state().lock().map_err(|e| {
            PyRuntimeError::new_err(format!("cache_insert: state lock poisoned: {e}"))
        })?;
        let Some(conn) = guard.conn.as_mut() else {
            // No DB — fail-open; the call is opt-in, a miss is safe.
            return Ok(());
        };
        if let Err(e) = agentc_memo::ffi::insert(
            conn,
            prompt_hash,
            model,
            parameters_hash,
            call_site_id,
            output_bytes,
            input_tokens,
            output_tokens,
            recorded_cost_usd,
            ttl_seconds,
            embedding_vec.as_deref(),
        ) {
            eprintln!("agentc: cache_insert failed: {e}");
        }
        Ok(())
    })
}

/// Decode a 256×f32 embedding from little-endian bytes.
///
/// Returns `None` when the length is wrong so the rest of the FFI call can
/// continue without the embedding — a dropped LSH write is always safer than
/// an aborted cache insert.
fn decode_embedding_bytes(bytes: &[u8]) -> Option<Vec<f32>> {
    const EMBED_BYTES: usize = agentc_embed::EMBEDDING_DIM * 4;
    if bytes.len() != EMBED_BYTES {
        return None;
    }
    let mut out = Vec::with_capacity(agentc_embed::EMBEDDING_DIM);
    for chunk in bytes.chunks_exact(4) {
        out.push(f32::from_le_bytes([chunk[0], chunk[1], chunk[2], chunk[3]]));
    }
    Some(out)
}

/// Invalidate cache entries by `call_site_id` GLOB pattern (e.g. `app.router:*`).
/// Pass `"*"` to wipe everything. Returns the number of rows removed.
#[pyfunction]
fn cache_invalidate(py: Python<'_>, pattern: &str) -> PyResult<u64> {
    py.allow_threads(|| {
        let guard = state().lock().map_err(|e| {
            PyRuntimeError::new_err(format!("cache_invalidate: state lock poisoned: {e}"))
        })?;
        let Some(conn) = guard.conn.as_ref() else {
            return Ok(0u64);
        };
        let pat = if pattern == "*" {
            InvalidationPattern::All
        } else {
            InvalidationPattern::CallSiteGlob(pattern.to_string())
        };
        Ok(agentc_memo::ffi::invalidate(conn, pat))
    })
}

/// Return aggregate cache statistics.
#[pyfunction]
fn cache_stats(py: Python<'_>) -> PyResult<Py<PyDict>> {
    let stats = py.allow_threads(|| -> agentc_memo::CacheStats {
        let Ok(guard) = state().lock() else {
            return agentc_memo::CacheStats::default();
        };
        let Some(conn) = guard.conn.as_ref() else {
            return agentc_memo::CacheStats::default();
        };
        agentc_memo::ffi::stats(conn)
    });

    let d = PyDict::new_bound(py);
    d.set_item("entries", stats.entries)?;
    d.set_item("total_hits", stats.total_hits)?;
    d.set_item("estimated_savings_usd", stats.estimated_savings_usd)?;
    d.set_item("bytes_on_disk", stats.bytes_on_disk)?;
    Ok(d.unbind())
}

/// Run the memoization cache's maintenance pass (TTL + LRU + VACUUM).
///
/// Returns a dict with keys `ttl_rows`, `lru_rows`, `vacuumed`. Invoked
/// periodically from the Python writer thread. Fail-open: every internal
/// failure is swallowed and the corresponding stat is 0.
#[pyfunction]
#[pyo3(signature = (max_entries=0))]
fn cache_maintenance(py: Python<'_>, max_entries: u64) -> PyResult<Py<PyDict>> {
    let (ttl_rows, lru_rows, vacuumed) = py.allow_threads(|| -> (u64, u64, bool) {
        let Ok(guard) = state().lock() else {
            return (0, 0, false);
        };
        let Some(conn) = guard.conn.as_ref() else {
            return (0, 0, false);
        };
        agentc_memo::ffi::maintenance(conn, max_entries)
    });

    let d = PyDict::new_bound(py);
    d.set_item("ttl_rows", ttl_rows)?;
    d.set_item("lru_rows", lru_rows)?;
    d.set_item("vacuumed", vacuumed)?;
    Ok(d.unbind())
}

/// Load a row from the shared `output_content` table by its content_id.
///
/// Returns the raw bytes the caller stashed at insert time (for memoization
/// that's a pickle payload; for spans it's a compressed JSON body). `None`
/// if the row is missing or the DB is not open — fail-open, the caller
/// retries the original operation.
#[pyfunction]
fn output_content_load<'py>(
    py: Python<'py>,
    content_id: &str,
) -> PyResult<Option<Bound<'py, PyBytes>>> {
    let content_id = content_id.to_string();
    let result = py.allow_threads(|| -> Option<Vec<u8>> {
        let local = {
            let guard = state().lock().ok()?;
            let conn = guard.conn.as_ref()?;
            conn.query_row(
                "SELECT content_text FROM output_content WHERE content_id = ?1",
                rusqlite::params![content_id],
                |row| row.get::<_, Vec<u8>>(0),
            )
            .ok()
        };
        if local.is_some() {
            return local;
        }
        // Read merged payloads with the same SQLite library as their writer;
        // do not create a missing store or hold the capture mutex during I/O.
        let canonical = Connection::open_with_flags(
            agentc_core::merge::canonical_db_path(),
            rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
        )
        .ok()?;
        canonical
            .query_row(
                "SELECT content_text FROM output_content WHERE content_id = ?1",
                rusqlite::params![content_id],
                |row| row.get::<_, Vec<u8>>(0),
            )
            .ok()
    });
    Ok(result.map(|bytes| PyBytes::new_bound(py, &bytes)))
}

/// Embed `text` and return the 256 × f32 little-endian bytes expected by
/// `cache_lookup` / `cache_insert`. Returns `None` if the embedder is
/// unavailable — the decorator treats that as "skip LSH".
#[pyfunction]
fn embed_text_bytes<'py>(py: Python<'py>, text: &str) -> PyResult<Option<Bound<'py, PyBytes>>> {
    let embedding = py.allow_threads(|| agentc_embed::embed_text_f32(text));
    let Some(vec) = embedding else {
        return Ok(None);
    };
    let mut buf = Vec::with_capacity(vec.len() * 4);
    for v in vec {
        buf.extend_from_slice(&v.to_le_bytes());
    }
    Ok(Some(PyBytes::new_bound(py, &buf)))
}

/// Canonicalize a prompt using the Rust mirror adapter.
///
/// Accepts a JSON-encoded prompt (bytes) and a provider tag; returns the
/// canonical UTF-8 JSON bytes. Used by parity tests to confirm Python and
/// Rust canonicalizers agree.
#[pyfunction]
fn canonicalize_prompt_bytes<'py>(
    py: Python<'py>,
    prompt_json: &[u8],
    provider: &str,
) -> PyResult<Bound<'py, PyBytes>> {
    let value: serde_json::Value = serde_json::from_slice(prompt_json)
        .map_err(|e| PyValueError::new_err(format!("invalid JSON: {e}")))?;
    let bytes = agentc_memo::canonical::canonicalize_prompt(&value, provider);
    Ok(PyBytes::new_bound(py, &bytes))
}

/// Canonicalize parameters using the Rust mirror adapter.
#[pyfunction]
fn canonicalize_parameters_bytes<'py>(
    py: Python<'py>,
    params_json: &[u8],
) -> PyResult<Bound<'py, PyBytes>> {
    let value: serde_json::Value = serde_json::from_slice(params_json)
        .map_err(|e| PyValueError::new_err(format!("invalid JSON: {e}")))?;
    let bytes = agentc_memo::canonical::canonicalize_parameters(&value);
    Ok(PyBytes::new_bound(py, &bytes))
}

/// Process-global optimizer slot. Direct FFI callers initialize it lazily;
/// the Python lifecycle explicitly replaces and resets it so repeated
/// ``init(storage_path=...)`` calls cannot share warm state across stores.
/// Each installed state holds:
///
/// - A fully-wired `Optimizer` with all five rewrite rules.
/// - The `CostModel` and `Budget` warmed from `cost_model.db` on init.
/// - Complete-plan profiles warmed from their exact retained windows.
/// - A `Mutex<Connection>` for `optimizer_audit.db`. We write `plan_audit`
///   rows synchronously from the hot path; SQLite WAL mode keeps the
///   per-row latency well under a millisecond.
/// - An `AtomicU64` observe counter so we can periodically flush dirty cost
///   profiles without spawning a thread. Shadow divergence is persisted on
///   each sampled comparison and flushed again at lifecycle boundaries.
///
/// On any wiring failure (e.g. corrupted `cost_model.db`) we fall back to
/// an empty optimizer — the user's LLM call must never break because the
/// optimizer itself failed to initialize.
struct OptimizerState {
    optimizer: Arc<Optimizer>,
    cost_model: Arc<CostModel>,
    plan_profiles: Arc<PlanProfiles>,
    plan_guard: Arc<PlanGuard>,
    model_catalog: Option<Arc<ModelCatalog>>,
    exploration_controller: ExplorationController,
    exploration_enabled: bool,
    evaluation_planner_mode: EvaluationPlannerMode,
    selection_audit_enabled: bool,
    #[allow(dead_code)]
    budget: Arc<Budget>,
    audit: Option<Mutex<Connection>>,
    cost_db: Option<Mutex<Connection>>,
    observe_counter: std::sync::atomic::AtomicU64,
    storage_dir: std::path::PathBuf,
}

static OPTIMIZER: OnceLock<RwLock<Option<Arc<OptimizerState>>>> = OnceLock::new();

/// Flush cost and complete-plan profiles to `cost_model.db` every N observes.
/// 16 is a compromise between losing too many in-flight samples on a crash and
/// hammering SQLite for every observe; bench runs do ~100 observes, so 16
/// → ~6 flushes per run.
const COST_MODEL_FLUSH_EVERY: u64 = 16;

fn resolve_storage_dir() -> std::path::PathBuf {
    agentc_core::merge::agentc_data_dir()
}

fn optimizer_slot() -> &'static RwLock<Option<Arc<OptimizerState>>> {
    OPTIMIZER.get_or_init(|| RwLock::new(None))
}

fn build_optimizer_state(storage: std::path::PathBuf, config: OptimizerConfig) -> OptimizerState {
    let evaluation_planner_mode = evaluation_planner_mode_from_env();
    let selection_audit_enabled = std::env::var("AGENTC_OPTIMIZE_SELECTION_AUDIT")
        .ok()
        .is_some_and(|value| matches!(value.trim().to_ascii_lowercase().as_str(), "1" | "true"));
    // Resolve once per lifecycle. Invalid allocation disables optional probes
    // while leaving primary execution and all durable budgets intact.
    let exploration_enabled = config.enabled && config.exploration.enabled;
    let exploration_controller =
        ExplorationController::with_policy(config.exploration_policy()).unwrap_or_default();
    match build_optimizer(&storage, config.clone()) {
        Ok(Wired {
            optimizer,
            cost_model,
            plan_profiles,
            plan_guard,
            model_catalog,
            budget,
            audit_conn,
        }) => {
            // Reopen cost DB for periodic flush — the `Wired::audit_conn`
            // is for the audit table; `cost_model.db` is a separate file.
            let cost_db = Connection::open(storage.join("cost_model.db"))
                .ok()
                .map(Mutex::new);
            if let Some(database) = &cost_db {
                if let Ok(mut conn) = database.lock() {
                    if let Err(error) =
                        agentc_optimizer::provider_reconciliation::recover_pending(&mut conn)
                    {
                        eprintln!(
                            "[agentc-profiler] billing reconciliation recovery failed: {error}"
                        );
                    }
                    if let Err(error) = agentc_optimizer::provider_publication::recover_pending(
                        &mut conn,
                        &plan_profiles,
                    ) {
                        eprintln!(
                            "[agentc-profiler] provider publication recovery failed: {error}"
                        );
                    }
                    if let Err(error) = agentc_optimizer::multi_call_comparison::replay_pairs(
                        &mut conn,
                        &plan_profiles,
                        &plan_guard,
                    ) {
                        eprintln!("[agentc-profiler] map comparison recovery failed: {error}");
                    }
                }
            }
            OptimizerState {
                optimizer,
                cost_model,
                plan_profiles,
                plan_guard,
                model_catalog: Some(model_catalog),
                exploration_controller,
                exploration_enabled,
                evaluation_planner_mode,
                selection_audit_enabled,
                budget,
                audit: Some(Mutex::new(audit_conn)),
                cost_db,
                observe_counter: std::sync::atomic::AtomicU64::new(0),
                storage_dir: storage,
            }
        }
        Err(e) => {
            eprintln!("[agentc-profiler] optimizer wiring failed: {e}");
            let cost_model = Arc::new(CostModel::with_window(config.cost_model_window));
            let plan_profiles = Arc::new(PlanProfiles::with_window(config.plan_profile_window));
            let plan_guard = Arc::new(PlanGuard::new());
            let budget = Arc::new(Budget::with_window(config.divergence_window));
            let optimizer = Arc::new(Optimizer::empty(cost_model.clone(), config));
            OptimizerState {
                optimizer,
                cost_model,
                plan_profiles,
                plan_guard,
                model_catalog: default_model_catalog().ok().map(Arc::new),
                exploration_controller,
                exploration_enabled,
                evaluation_planner_mode,
                selection_audit_enabled,
                budget,
                audit: None,
                cost_db: None,
                observe_counter: std::sync::atomic::AtomicU64::new(0),
                storage_dir: storage,
            }
        }
    }
}

fn optimizer_state() -> Arc<OptimizerState> {
    if let Ok(slot) = optimizer_slot().read() {
        if let Some(state) = slot.as_ref() {
            return Arc::clone(state);
        }
    }

    let mut slot = optimizer_slot()
        .write()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    if let Some(state) = slot.as_ref() {
        return Arc::clone(state);
    }
    let storage = resolve_storage_dir();
    let config = OptimizerConfig::from_storage(&storage);
    let state = Arc::new(build_optimizer_state(storage, config));
    *slot = Some(Arc::clone(&state));
    state
}

fn flush_optimizer_state(state: &OptimizerState) {
    if let Some(mu) = state.cost_db.as_ref() {
        let _ = std::panic::catch_unwind(AssertUnwindSafe(|| {
            if let Ok(mut conn) = mu.lock() {
                if let Err(e) = state.cost_model.flush_dirty(&mut conn) {
                    eprintln!("[agentc-profiler] cost_model flush failed: {e}");
                }
                if let Err(e) = state.plan_profiles.flush_dirty(&mut conn) {
                    eprintln!("[agentc-profiler] plan_profiles flush failed: {e}");
                }
                if let Err(e) = state.plan_guard.flush_dirty(&mut conn) {
                    eprintln!("[agentc-profiler] plan_guard flush failed: {e}");
                }
                if let Err(e) = state.budget.flush_divergence(&mut conn) {
                    eprintln!("[agentc-profiler] divergence flush failed: {e}");
                }
            }
        }));
    }
}

/// Configure a fresh optimizer at the lifecycle's resolved storage path.
/// Replaces any prior process-local optimizer after flushing its dirty state.
#[pyfunction(signature = (storage_path, config_path=None))]
fn optimize_configure(py: Python<'_>, storage_path: &str, config_path: Option<&str>) -> String {
    let storage = std::path::PathBuf::from(storage_path);
    py.allow_threads(|| {
        let mut slot = optimizer_slot()
            .write()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if let Some(previous) = slot.as_ref() {
            flush_optimizer_state(previous);
        }
        let config = config_path
            .map(std::path::Path::new)
            .map(OptimizerConfig::from_file)
            .unwrap_or_else(|| OptimizerConfig::from_storage(&storage));
        let state = Arc::new(build_optimizer_state(storage, config));
        let resolved = state.storage_dir.to_string_lossy().into_owned();
        *slot = Some(state);
        resolved
    })
}

#[pyfunction]
fn optimize_configuration() -> String {
    let state = optimizer_state();
    serde_json::json!({"configuration": state.optimizer.config(),
        "configuration_error": state.optimizer.config().configuration_error})
    .to_string()
}

/// Return the storage path owned by the current native optimizer.
#[pyfunction]
fn optimize_storage_path() -> String {
    optimizer_state().storage_dir.to_string_lossy().into_owned()
}

/// Drop the lifecycle-owned optimizer so a later init may choose another path.
#[pyfunction]
fn optimize_reset(py: Python<'_>) {
    py.allow_threads(|| {
        let previous = optimizer_slot()
            .write()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .take();
        if let Some(state) = previous {
            flush_optimizer_state(&state);
        }
    });
}

fn now_us_i64() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_micros() as i64)
        .unwrap_or(0)
}

fn guard_plan_fail_safe<F>(guard_plan: F) -> String
where
    F: FnOnce() -> String,
{
    std::panic::catch_unwind(AssertUnwindSafe(guard_plan))
        .unwrap_or_else(|_| PASS_THROUGH_JSON.to_string())
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum EvaluationPlannerMode {
    JointGuarded,
    CurrentGreedy,
    Frozen(agentc_optimizer::calibrated_policy::Mode),
    Invalid,
}

fn evaluation_planner_mode_from_env() -> EvaluationPlannerMode {
    match std::env::var("AGENTC_EVAL_PLANNER_MODE")
        .ok()
        .as_deref()
        .map(str::trim)
    {
        None | Some("") | Some("joint_guarded") => EvaluationPlannerMode::JointGuarded,
        Some("current_greedy") => EvaluationPlannerMode::CurrentGreedy,
        Some("best_static_joint") => EvaluationPlannerMode::Frozen(
            agentc_optimizer::calibrated_policy::Mode::BestStaticJoint,
        ),
        Some("route_then_rewrite") => EvaluationPlannerMode::Frozen(
            agentc_optimizer::calibrated_policy::Mode::RouteThenRewrite,
        ),
        Some("rewrite_then_route") => EvaluationPlannerMode::Frozen(
            agentc_optimizer::calibrated_policy::Mode::RewriteThenRoute,
        ),
        Some(_) => EvaluationPlannerMode::Invalid,
    }
}

/// Report the text action contract supported by the compiled identity/lease gate.
#[pyfunction]
fn optimize_text_action_comparator_version() -> &'static str {
    agentc_optimizer::ffi::TEXT_ACTION_COMPARATOR_VERSION
}

#[pyfunction]
fn optimize_evaluation_policy_version() -> &'static str {
    agentc_optimizer::calibrated_policy::VERSION
}

#[pyfunction]
fn optimize_provider_configuration_version() -> &'static str {
    agentc_optimizer::model_catalog::PROVIDER_CONFIGURATION_VERSION
}

#[pyfunction]
fn optimize_application_version() -> &'static str {
    agentc_optimizer::model_catalog::APPLICATION_VERSION
}

#[pyfunction]
fn optimize_output_validation_version() -> &'static str {
    agentc_optimizer::model_catalog::OUTPUT_VALIDATION_VERSION
}

#[pyfunction]
fn optimize_record_output_validation_rejection(
    py: Python<'_>,
    plan_json: &str,
    contract_json: &str,
    execution_id: &str,
) -> bool {
    py.allow_threads(|| {
        std::panic::catch_unwind(AssertUnwindSafe(|| {
            let state = optimizer_state();
            let Some(mutex) = state.cost_db.as_ref() else {
                return false;
            };
            let Ok(mut conn) = mutex.lock() else {
                return false;
            };
            match agentc_optimizer::ffi::record_output_validation_rejection(
                &state.plan_profiles,
                &state.plan_guard,
                &mut conn,
                plan_json,
                contract_json,
                execution_id,
                now_us_i64(),
            ) {
                Ok(()) => true,
                Err(error) => {
                    eprintln!("[agentc-profiler] validation rejection persistence failed: {error}");
                    false
                }
            }
        }))
        .unwrap_or(false)
    })
}

#[pyfunction]
fn optimize_divergence_metric_version(mode: &str) -> &'static str {
    agentc_optimizer::ffi::divergence_metric_version(mode)
}

/// Pure request-key projection; does not initialize storage or the optimizer.
#[pyfunction]
fn optimize_sdk_response_cache_key(call_json: &str) -> Option<String> {
    let call: agentc_optimizer::Call = serde_json::from_str(call_json).ok()?;
    let key = agentc_optimizer::wiring::sdk_response_cache_key(&call)?;
    let request = agentc_optimizer::RequestRequirements::from_call(&call)?;
    serde_json::to_string(&serde_json::json!({
        "version": agentc_optimizer::wiring::SDK_RESPONSE_CACHE_VERSION,
        "prompt_hash": key.prompt_hash_hex(), "parameters_hash": key.parameters_hash_hex(),
        "model": key.model, "call_site_id": key.call_site_id,
        "provider_protocol": request.provider_protocol, "provider_namespace": request.provider_namespace,
    })).ok()
}

/// Return the evaluation mode frozen when this optimizer was configured.
#[pyfunction]
fn optimize_evaluation_planner_mode() -> &'static str {
    match optimizer_state().evaluation_planner_mode {
        EvaluationPlannerMode::JointGuarded => "joint_guarded",
        EvaluationPlannerMode::CurrentGreedy => "current_greedy",
        EvaluationPlannerMode::Frozen(mode) => mode.as_str(),
        EvaluationPlannerMode::Invalid => "invalid",
    }
}

fn projected_greedy_plan(state: &OptimizerState, call_json: &str, now_us: i64) -> String {
    let plan_json = rust_projected_plan(&state.optimizer, call_json);
    let threshold = serde_json::from_str::<Plan>(&plan_json)
        .ok()
        .map(|plan| state.optimizer.divergence_threshold_for_plan(&plan))
        .unwrap_or(0.05);
    let guarded = guard_and_attach_observation_context(
        state.model_catalog.as_deref(),
        &state.plan_guard,
        call_json,
        &plan_json,
        threshold,
        now_us,
    );
    unavailable_selection_audit(
        guarded,
        "current_greedy",
        "complete_plans_not_enumerated",
        state.selection_audit_enabled,
    )
}

/// Plan an intercepted LLM call. JSON-in, JSON-out; any panic on the Rust
/// side is swallowed and the caller receives `{"kind":"pass_through"}`.
///
/// Fail-open is a hard requirement: a user's LLM call must never raise
/// because the optimizer itself crashed.
///
/// Side effect: writes one row to `optimizer_audit.db::plan_audit` per
/// invocation (synchronous; lock-protected). Audit failures are logged
/// but never propagated.
#[pyfunction]
fn optimize_plan(py: Python<'_>, call_json: &str) -> String {
    py.allow_threads(|| {
        let state = optimizer_state();
        let t0 = std::time::Instant::now();
        let bound_call = agentc_optimizer::ffi::bind_sampling_policy(
            call_json,
            state.optimizer.config().shadow_rate,
        );
        let call_json = bound_call.as_str();
        let max_overhead_us = if state.optimizer.config().max_overhead_ms.is_finite() {
            (state.optimizer.config().max_overhead_ms.max(0.0) * 1_000.0) as u128
        } else {
            0
        };
        let primary_plan_json = guard_plan_fail_safe(|| match state.evaluation_planner_mode {
            EvaluationPlannerMode::CurrentGreedy => {
                projected_greedy_plan(&state, call_json, now_us_i64())
            }
            EvaluationPlannerMode::Invalid => PASS_THROUGH_JSON.to_string(),
            EvaluationPlannerMode::Frozen(mode) => rust_frozen_policy(
                &state.optimizer,
                state.model_catalog.as_deref(),
                &state.plan_guard,
                call_json,
                mode,
                now_us_i64(),
                state.selection_audit_enabled,
            ),
            EvaluationPlannerMode::JointGuarded => rust_profiled_plan(
                &state.optimizer,
                state.model_catalog.as_deref(),
                &state.plan_profiles,
                &state.plan_guard,
                call_json,
                now_us_i64(),
                state.selection_audit_enabled,
            ),
        });
        let plan_json = if state.evaluation_planner_mode == EvaluationPlannerMode::JointGuarded
            && state.exploration_enabled
            && t0.elapsed().as_micros() <= max_overhead_us
        {
            state
                .cost_db
                .as_ref()
                // Exploration is optional calibration work. Never make a user
                // call wait behind a background completion holding the DB.
                .and_then(|mutex| mutex.try_lock().ok())
                .map(|mut connection| {
                    if t0.elapsed().as_micros() > max_overhead_us {
                        return primary_plan_json.clone();
                    }
                    guard_plan_fail_safe(|| {
                        let explored = rust_reserve_exploration(
                            &state.optimizer,
                            state.model_catalog.as_deref(),
                            &state.plan_profiles,
                            &state.plan_guard,
                            &state.exploration_controller,
                            &mut connection,
                            call_json,
                            &primary_plan_json,
                            now_us_i64(),
                        );
                        if explored != primary_plan_json
                            && t0.elapsed().as_micros() > max_overhead_us
                        {
                            // No envelope has crossed into Python/provider
                            // dispatch. Cancel this planning-only reservation
                            // without consuming the rolling provider-call cap.
                            let _ = rust_cancel_embedded_exploration(
                                &state.exploration_controller,
                                &mut connection,
                                &explored,
                            );
                            primary_plan_json.clone()
                        } else {
                            explored
                        }
                    })
                })
                .unwrap_or_else(|| primary_plan_json.clone())
        } else {
            primary_plan_json
        };
        let overhead_us = t0.elapsed().as_micros() as i64;

        // Best-effort audit. The plan is always returned regardless of
        // whether the audit row lands.
        let _ = std::panic::catch_unwind(AssertUnwindSafe(|| {
            write_plan_audit(&state, call_json, &plan_json, overhead_us);
        }));

        plan_json
    })
}

/// Measurement-only: one named rule's proposed rewrite of `call_json` under
/// the live call-site profile of the configured optimizer storage. Skips
/// admission, the hot threshold and the guard; writes no audit row and
/// records nothing. See `agentc_optimizer::ffi::propose_rule`.
#[pyfunction]
fn optimize_propose_rule(py: Python<'_>, call_json: &str, rule_name: &str) -> String {
    py.allow_threads(|| {
        let state = optimizer_state();
        agentc_optimizer::ffi::propose_rule(&state.optimizer, call_json, rule_name)
    })
}

#[pyfunction]
fn optimize_original_provider_call(py: Python<'_>, call_json: &str) -> String {
    py.allow_threads(|| {
        guard_plan_fail_safe(|| {
            let state = optimizer_state();
            agentc_optimizer::ffi::attach_observation_context(
                state.model_catalog.as_deref(), call_json, PASS_THROUGH_JSON, 0.0,
            )
        })
    })
}

/// Return the exact versioned target catalog owned by the active optimizer.
/// This is an introspection surface only; Rust remains the source of routing
/// candidates and Python must still verify each routed dispatch contract.
#[pyfunction]
fn optimize_model_catalog(py: Python<'_>) -> String {
    py.allow_threads(|| {
        let state = optimizer_state();
        state
            .model_catalog
            .as_ref()
            .and_then(|catalog| serde_json::to_string(catalog.as_ref()).ok())
            .unwrap_or_else(|| {
                "{\"catalog_version\":\"unavailable\",\"price_table_version\":\"unavailable\",\"observed_at_utc\":\"unavailable\",\"targets\":[]}".to_string()
            })
    })
}

/// Decode just enough of the call/plan to log one audit row. Anything
/// missing is treated as "pass-through, no rule" — we'd rather log a
/// partial truth than skip the row entirely.
fn write_plan_audit(state: &OptimizerState, call_json: &str, plan_json: &str, overhead_us: i64) {
    let Some(audit_mu) = state.audit.as_ref() else {
        return;
    };

    // Pull call_site_id + span_id from the call. If the call is malformed
    // we already returned PassThrough; record it as such with empty IDs so
    // the audit row count still tracks invocations.
    let (call_site_id, span_id) = match serde_json::from_str::<serde_json::Value>(call_json) {
        Ok(v) => {
            let site = v
                .get("call_site_id")
                .and_then(|x| x.as_str())
                .unwrap_or("")
                .to_string();
            let span = v
                .get("span_id")
                .and_then(|x| x.as_str())
                .map(|s| decode_hex8(s).unwrap_or([0u8; 8]))
                .unwrap_or([0u8; 8]);
            (site, span)
        }
        Err(_) => (String::new(), [0u8; 8]),
    };

    let plan: Plan = match serde_json::from_str(plan_json) {
        Ok(p) => p,
        Err(_) => Plan::PassThrough,
    };

    let (plan_kind, rule, projected) = match &plan {
        Plan::PassThrough => (PlanKind::PassThrough, None, None),
        Plan::Cached { .. } => (PlanKind::Cached, None, None),
        Plan::Rewritten {
            rule,
            projected_savings_usd,
            ..
        } => (
            PlanKind::Rewritten,
            Some(rule.clone()),
            Some(*projected_savings_usd as f64),
        ),
        Plan::Parallel {
            rule,
            projected_savings_usd,
            ..
        } => (
            PlanKind::Parallel,
            Some(rule.clone()),
            Some(*projected_savings_usd as f64),
        ),
        Plan::Composed {
            rules,
            net_savings_usd,
            ..
        } => (
            PlanKind::Composed,
            rules.first().map(|r| r.rule.clone()),
            Some(*net_savings_usd as f64),
        ),
    };

    let row = PlanAudit {
        ts_us: now_us_i64(),
        call_site_id,
        span_id,
        plan_kind,
        rule,
        projected_savings_usd: projected,
        measured_savings_usd: None,
        overhead_us,
        shadow_sampled: false,
        shadow_divergence: None,
    };

    let Ok(conn) = audit_mu.lock() else {
        return;
    };
    if let Err(e) = audit_insert(&conn, &row) {
        eprintln!("[agentc-profiler] plan_audit insert failed: {e}");
    }
}

fn decode_hex8(s: &str) -> Option<[u8; 8]> {
    if s.len() != 16 {
        return None;
    }
    let mut out = [0u8; 8];
    for (i, b) in out.iter_mut().enumerate() {
        let hi = (s.as_bytes().get(2 * i)?).to_ascii_lowercase();
        let lo = (s.as_bytes().get(2 * i + 1)?).to_ascii_lowercase();
        *b = (hex_nib(hi)? << 4) | hex_nib(lo)?;
    }
    Some(out)
}

fn hex_nib(c: u8) -> Option<u8> {
    match c {
        b'0'..=b'9' => Some(c - b'0'),
        b'a'..=b'f' => Some(c - b'a' + 10),
        _ => None,
    }
}

/// Fold a dispatched plan's measured outcome into the cost model. Never
/// raises — errors are silently dropped because the user-visible call has
/// already completed.
///
/// Periodically flushes the cost-model `dirty` set to `cost_model.db` so
/// the next process can warm up without re-observing every hot site.
#[pyfunction]
fn optimize_observe(py: Python<'_>, plan_json: &str, outcome_json: &str) -> String {
    py.allow_threads(|| {
        let state = optimizer_state();
        let observation_token = match std::panic::catch_unwind(AssertUnwindSafe(|| {
            rust_observe(
                &state.cost_model,
                &state.plan_profiles,
                plan_json,
                outcome_json,
            )
        })) {
            Ok(Ok(token)) => token,
            Ok(Err(error)) => {
                eprintln!("[agentc-profiler] optimizer observation failed: {error}");
                String::new()
            }
            Err(_) => String::new(),
        };

        let n = state
            .observe_counter
            .fetch_add(1, std::sync::atomic::Ordering::Relaxed)
            + 1;
        if n.is_multiple_of(COST_MODEL_FLUSH_EVERY) {
            if let Some(mu) = state.cost_db.as_ref() {
                let _ = std::panic::catch_unwind(AssertUnwindSafe(|| {
                    if let Ok(mut conn) = mu.lock() {
                        if let Err(e) = state.cost_model.flush_dirty(&mut conn) {
                            eprintln!("[agentc-profiler] cost_model flush failed: {e}");
                        }
                        if let Err(e) = state.plan_profiles.flush_dirty(&mut conn) {
                            eprintln!("[agentc-profiler] plan_profiles flush failed: {e}");
                        }
                        if let Err(e) = state.plan_guard.flush_dirty(&mut conn) {
                            eprintln!("[agentc-profiler] plan_guard flush failed: {e}");
                        }
                    }
                }));
            }
        }
        observation_token
    })
}

/// Persist one provider-dispatch journal operation without manufacturing plan evidence.
#[pyfunction]
fn optimize_record_provider_receipt(py: Python<'_>, record_json: &str) -> String {
    py.allow_threads(|| {
        let state = optimizer_state();
        let result = std::panic::catch_unwind(AssertUnwindSafe(|| {
            let database = state.cost_db.as_ref().ok_or_else(|| "provider receipt store is unavailable".to_string())?;
            let mut conn = database.lock().map_err(|_| "provider receipt store is unavailable".to_string())?;
            let command: serde_json::Value = serde_json::from_str(record_json).map_err(|error| error.to_string())?;
            if command.get("operation").and_then(serde_json::Value::as_str) == Some("begin_call") {
                let config = state.optimizer.config();
                let policy = agentc_optimizer::provider_graph_quality::Policy {
                    divergence_threshold: config.selection.global_divergence_threshold.unwrap_or(0.0),
                    exposure_budget: config.selection.divergence_exposure_budget,
                    min_pairs: config.selection.min_plan_evidence.max(20) as usize,
                };
                let batch = command.get("batch_id").and_then(serde_json::Value::as_str).ok_or("batch identity missing")?;
                agentc_optimizer::provider_graph_role_comparison::verify_dispatch_policy(&conn, batch, policy, state.plan_profiles.retained_window_size()).map_err(|e|e.to_string())?;
                agentc_optimizer::provider_graph_deployment::verify_dispatch_policy(&conn, batch, policy, &config.selection_policy(now_us_i64()), state.plan_profiles.retained_window_size()).map_err(|e|e.to_string())?;
            }
            if command.get("operation").and_then(serde_json::Value::as_str) == Some("begin_graph_deployment") {
                let config=state.optimizer.config();
                let policy=agentc_optimizer::provider_graph_quality::Policy {divergence_threshold:config.selection.global_divergence_threshold.unwrap_or(0.0),exposure_budget:config.selection.divergence_exposure_budget,min_pairs:config.selection.min_plan_evidence.max(20) as usize};
                let request=serde_json::from_value(command.get("request").cloned().ok_or("deployment request missing")?).map_err(|e|e.to_string())?;
                agentc_optimizer::provider_graph_deployment::validate_sdk_policy(&request,policy,&config.selection_policy(now_us_i64()),state.plan_profiles.retained_window_size()).map_err(|e|e.to_string())?;
            }
            if command.get("operation").and_then(serde_json::Value::as_str) == Some("finish_primary_graph_deployment") {
                if command.as_object().is_none_or(|value|value.len()!=3) { return Err("primary completion accepts its original lease and callback result".into()); }
                let lease=command.get("lease_id").and_then(serde_json::Value::as_str).ok_or("primary lease identity missing")?;
                let comparison=serde_json::from_value(command.get("comparison").cloned().ok_or("primary callback result missing")?).map_err(|e|e.to_string())?;
                let config=state.optimizer.config();
                let policy=agentc_optimizer::provider_graph_quality::Policy {divergence_threshold:config.selection.global_divergence_threshold.unwrap_or(0.0),exposure_budget:config.selection.divergence_exposure_budget,min_pairs:config.selection.min_plan_evidence.max(20) as usize};
                let prepared=agentc_optimizer::provider_graph_preparation::load(&conn,lease).map_err(|e|e.to_string())?;
                agentc_optimizer::provider_graph_deployment::verify_dispatch_policy(&conn,&prepared.batch_id,policy,&config.selection_policy(now_us_i64()),state.plan_profiles.retained_window_size()).map_err(|e|e.to_string())?;
                let execution=agentc_optimizer::provider_graph_deployment::finish_primary(&mut conn,&state.plan_profiles,lease,comparison,now_us_i64()).map_err(|e|e.to_string())?;
                return Ok(serde_json::json!({"status":"recorded","execution":execution,"admission":"guarded_primary_deployment"}).to_string());
            }
            if command.get("operation").and_then(serde_json::Value::as_str) == Some("original_provider_execution") {
                if command.as_object().is_none_or(|value| value.len()!=2) {
                    return Err("original execution lookup accepts only its batch identity".into());
                }
                let batch=command.get("batch_id").and_then(serde_json::Value::as_str).ok_or("batch identity missing")?;
                let execution=agentc_optimizer::provider_execution::register(&conn,batch).map_err(|error|error.to_string())?;
                return Ok(serde_json::json!({"status":"recorded","execution":execution,"admission":"observe_only"}).to_string());
            }
            if command.get("operation").and_then(serde_json::Value::as_str) == Some("compare_original_provider_executions") {
                if command.as_object().is_none_or(|value| value.len()!=3) {
                    return Err("original comparison accepts only its two original execution identities".into());
                }
                let reference=command.get("reference_execution_id").and_then(serde_json::Value::as_str).ok_or("reference identity missing")?;
                let candidate=command.get("candidate_execution_id").and_then(serde_json::Value::as_str).ok_or("candidate identity missing")?;
                let at=std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_err(|error|error.to_string())?.as_micros().min(i64::MAX as u128) as i64;
                let pair=agentc_optimizer::provider_execution::compare(&mut conn,reference,candidate,at).map_err(|error|error.to_string())?;
                return Ok(serde_json::json!({"status":"recorded","pair":pair,"admission":"observe_only"}).to_string());
            }
            if command.get("operation").and_then(serde_json::Value::as_str) == Some("original_provider_quality") {
                if command.as_object().is_none_or(|value|value.len()!=2) { return Err("original quality lookup accepts only its batch identity".into()); }
                let batch=command.get("batch_id").and_then(serde_json::Value::as_str).ok_or("batch identity missing")?;
                let execution=agentc_optimizer::provider_execution::register(&conn,batch).map_err(|e|e.to_string())?;
                let quality=if let Some(execution)=&execution {
                    agentc_optimizer::provider_quality::window(&conn,&execution.key,&execution.runtime_version,state.plan_profiles.retained_window_size(),now_us_i64()).map_err(|e|e.to_string())?
                } else { None };
                return Ok(serde_json::json!({"status":"recorded","execution":execution,"quality_window":quality,"admission":"observe_only"}).to_string());
            }
            if command.get("operation").and_then(serde_json::Value::as_str) == Some("tariff_plan_profile") {
                if command.as_object().is_none_or(|value| value.len() != 2) {
                    return Err("tariff plan report accepts only the original batch identity".into());
                }
                let id = command.get("batch_id").and_then(serde_json::Value::as_str).ok_or("batch identity missing")?;
                let observation = agentc_optimizer::provider_interval_profiles::publish(&mut conn, id).map_err(|error|error.to_string())?;
                let window = if let Some(observation) = &observation {
                    agentc_optimizer::provider_interval_profiles::window(
                        &conn, &observation.key, &observation.runtime_version, state.plan_profiles.retained_window_size(),
                    ).map_err(|error|error.to_string())?
                } else { None };
                return Ok(serde_json::json!({"status":"recorded","observation":observation,"window":window,
                    "quality_evidence":"separate_original_comparisons_required","admission":"observe_only"}).to_string());
            }
            if command.get("operation").and_then(serde_json::Value::as_str) == Some("publish_batch") {
                if command.as_object().is_none_or(|value| value.len() != 2) {
                    return Err("publication refresh accepts only the original batch identity".into());
                }
                let id = command.get("batch_id").and_then(serde_json::Value::as_str).ok_or("batch identity missing")?;
                return agentc_optimizer::provider_publication::publish(&mut conn, &state.plan_profiles, id).map_err(|error| error.to_string());
            }
            match agentc_optimizer::provider_receipts::handle_json(&mut conn, record_json) {
                Ok(value) => Ok(value),
                Err(error) => match agentc_optimizer::provider_graph_campaign::binding_abstention(&error) {
                    Some(value) => Ok(value),
                    None => Err(error.to_string()),
                },
            }
        }));
        match result {
            Ok(Ok(value)) => value,
            Ok(Err(reason)) => serde_json::json!({"status":"abstained","reason":reason}).to_string(),
            Err(_) => serde_json::json!({"status":"abstained","reason":"provider receipt persistence failed"}).to_string(),
        }
    })
}

/// Observe real ordered-map calls without exposing a single-call feedback
/// token or enabling automatic callback reordering/replay.
#[pyfunction]
fn optimize_observe_multi_call(py: Python<'_>, receipt_json: &str) -> String {
    py.allow_threads(|| {
        let state = optimizer_state();
        let result = std::panic::catch_unwind(AssertUnwindSafe(|| {
            let catalog = state
                .model_catalog
                .as_ref()
                .ok_or("map model catalog is unavailable")?;
            if let Some(database) = &state.cost_db {
                let mut conn = database
                    .lock()
                    .map_err(|_| "map comparison store is unavailable".to_string())?;
                agentc_optimizer::ffi::optimize_observe_multi_call_with_comparison(
                    catalog,
                    &state.plan_profiles,
                    &mut conn,
                    receipt_json,
                )
            } else {
                agentc_optimizer::ffi::optimize_observe_multi_call(
                    catalog,
                    &state.plan_profiles,
                    receipt_json,
                )
            }
        }));
        match result {
            Ok(Ok(receipt)) => {
                flush_optimizer_state(&state);
                receipt
            }
            Ok(Err(reason)) => {
                serde_json::json!({"status":"abstained","reason":reason}).to_string()
            }
            Err(_) => {
                serde_json::json!({"status":"abstained","reason":"native map observation failed"})
                    .to_string()
            }
        }
    })
}

#[pyfunction]
fn optimize_compare_multi_call(
    py: Python<'_>,
    reference_token: &str,
    candidate_token: &str,
) -> String {
    py.allow_threads(|| {
        let state = optimizer_state();
        let result = std::panic::catch_unwind(AssertUnwindSafe(|| -> Result<String, String> {
            let database = state
                .cost_db
                .as_ref()
                .ok_or_else(|| "map comparison store is unavailable".to_string())?;
            let mut conn = database
                .lock()
                .map_err(|_| "map comparison store is unavailable".to_string())?;
            let result = agentc_optimizer::multi_call_comparison::compare_executions(
                &mut conn,
                &state.plan_profiles,
                &state.plan_guard,
                reference_token,
                candidate_token,
                now_us_i64(),
            )
            .map_err(|error| error.to_string())?;
            serde_json::to_string(&result).map_err(|error| error.to_string())
        }));
        match result {
            Ok(Ok(receipt)) => receipt,
            Ok(Err(reason)) => {
                serde_json::json!({"status":"abstained","reason":reason.to_string()}).to_string()
            }
            Err(_) => {
                serde_json::json!({"status":"abstained","reason":"native map comparison failed"})
                    .to_string()
            }
        }
    })
}

#[pyfunction]
fn optimize_provider_graph_sampling(py: Python<'_>, request_json: &str) -> String {
    py.allow_threads(|| {
        let result = std::panic::catch_unwind(AssertUnwindSafe(|| -> Result<String, String> {
            let state = optimizer_state();
            let config = state.optimizer.config();
            let database = state.cost_db.as_ref().ok_or("sampling store is unavailable")?;
            let mut conn = database.lock().map_err(|_| "sampling store is unavailable")?;
            let command: agentc_optimizer::provider_graph_sampling::Command = serde_json::from_str(request_json).map_err(|e| e.to_string())?;
            let quality = agentc_optimizer::provider_graph_quality::Policy {
                divergence_threshold: config.selection.global_divergence_threshold.unwrap_or(0.0),
                exposure_budget: config.selection.divergence_exposure_budget,
                min_pairs: config.selection.min_plan_evidence.max(20) as usize,
            };
            let value = match command {
                agentc_optimizer::provider_graph_sampling::Command::Begin { request } => {
                    if !config.enabled { return Err("graph sampling requires the enabled optimizer".into()); }
                    if let Some(deployment) = &request.deployment {
                        agentc_optimizer::provider_graph_deployment::validate_sdk_admission(&deployment.policy,state.plan_profiles.retained_window_size(),quality,&config.selection_policy(now_us_i64()),state.plan_profiles.retained_window_size()).map_err(|e|e.to_string())?;
                    }
                    let outcome = agentc_optimizer::provider_graph_sampling::begin(&mut conn, *request, quality, state.plan_profiles.retained_window_size(), now_us_i64()).map_err(|e| e.to_string())?;
                    serde_json::to_value(outcome).map_err(|e| e.to_string())?
                }
                agentc_optimizer::provider_graph_sampling::Command::Finish { ticket_id } => {
                    let outcome = agentc_optimizer::provider_graph_sampling::finish(&mut conn, &ticket_id, now_us_i64(), quality, state.plan_profiles.retained_window_size()).map_err(|e| e.to_string())?;
                    serde_json::to_value(outcome).map_err(|e| e.to_string())?
                }
                agentc_optimizer::provider_graph_sampling::Command::Commitments { ticket_id } => {
                    let outcome=agentc_optimizer::provider_graph_sampling::commitment_report(&conn,&ticket_id).map_err(|e|e.to_string())?;
                    serde_json::json!({"commitments":outcome})
                }
            };
            Ok(serde_json::json!({"status":"recorded","sampling":value}).to_string())
        }));
        match result {
            Ok(Ok(value)) => value,
            Ok(Err(reason)) => serde_json::json!({"status":"abstained","reason":reason}).to_string(),
            Err(_) => serde_json::json!({"status":"abstained","reason":"native graph sampling failed"}).to_string(),
        }
    })
}

#[pyfunction]
fn optimize_compare_provider_graph(
    py: Python<'_>,
    reference_token: &str,
    candidate_token: &str,
) -> String {
    py.allow_threads(|| {
        let state = optimizer_state();
        let result = std::panic::catch_unwind(AssertUnwindSafe(|| -> Result<String, String> {
            let database = state.cost_db.as_ref().ok_or("graph comparison store is unavailable")?;
            let mut conn = database.lock().map_err(|_| "graph comparison store is unavailable")?;
            let config=state.optimizer.config();
            let policy=agentc_optimizer::provider_graph_quality::Policy {
                divergence_threshold:config.selection.global_divergence_threshold.unwrap_or(0.0),
                exposure_budget:config.selection.divergence_exposure_budget,
                min_pairs:config.selection.min_plan_evidence.max(20) as usize,
            };
            let outcome = agentc_optimizer::provider_graph_comparison::compare_with_policy(
                &mut conn, reference_token, candidate_token, now_us_i64(),
                state.plan_profiles.retained_window_size(),
                policy,
            ).map_err(|error| error.to_string())?;
            serde_json::to_string(&outcome).map_err(|error| error.to_string())
        }));
        match result {
            Ok(Ok(receipt)) => receipt,
            Ok(Err(reason)) => serde_json::json!({"status":"abstained","reason":reason}).to_string(),
            Err(_) => serde_json::json!({"status":"abstained","reason":"native graph comparison failed"}).to_string(),
        }
    })
}

#[pyfunction]
fn optimize_compare_provider_role_graph(
    py: Python<'_>,
    reference_token: &str,
    candidate_token: &str,
) -> String {
    py.allow_threads(|| {
        let state = optimizer_state();
        let result = std::panic::catch_unwind(AssertUnwindSafe(|| -> Result<String, String> {
            let database = state
                .cost_db
                .as_ref()
                .ok_or("graph comparison store is unavailable")?;
            let mut conn = database
                .lock()
                .map_err(|_| "graph comparison store is unavailable")?;
            let config = state.optimizer.config();
            let policy = agentc_optimizer::provider_graph_quality::Policy {
                divergence_threshold: config.selection.global_divergence_threshold.unwrap_or(0.0),
                exposure_budget: config.selection.divergence_exposure_budget,
                min_pairs: config.selection.min_plan_evidence.max(20) as usize,
            };
            let outcome = agentc_optimizer::provider_graph_role_comparison::compare_with_policy(
                &mut conn,
                reference_token,
                candidate_token,
                now_us_i64(),
                state.plan_profiles.retained_window_size(),
                policy,
            )
            .map_err(|error| error.to_string())?;
            serde_json::to_string(&outcome).map_err(|error| error.to_string())
        }));
        match result {
            Ok(Ok(receipt)) => receipt,
            Ok(Err(reason)) => {
                serde_json::json!({"status":"abstained","reason":reason}).to_string()
            }
            Err(_) => {
                serde_json::json!({"status":"abstained","reason":"native graph comparison failed"})
                    .to_string()
            }
        }
    })
}

#[pyfunction]
fn optimize_begin_provider_graph(py: Python<'_>, request_json: &str) -> String {
    py.allow_threads(|| {
        let result=std::panic::catch_unwind(AssertUnwindSafe(||->Result<String,String>{
            let state=optimizer_state();let config=state.optimizer.config();
            let policy=agentc_optimizer::provider_graph_quality::Policy {divergence_threshold:config.selection.global_divergence_threshold.unwrap_or(0.0),exposure_budget:config.selection.divergence_exposure_budget,min_pairs:config.selection.min_plan_evidence.max(20) as usize};
            let database=state.cost_db.as_ref().ok_or("graph preparation store is unavailable")?;
            let mut conn=database.lock().map_err(|_|"graph preparation store is unavailable")?;
            let request=serde_json::from_str(request_json).map_err(|e|e.to_string())?;
            let lease=match agentc_optimizer::provider_graph_preparation::begin(&mut conn,request,policy,state.plan_profiles.retained_window_size(),now_us_i64()) {
                Ok(lease) => lease,
                Err(error) => match agentc_optimizer::provider_graph_campaign::binding_abstention(&error) {
                    Some(value) => return Ok(value),
                    None => return Err(error.to_string()),
                },
            };
            Ok(serde_json::json!({"status":"recorded","lease_id":lease.lease_id,"started_at_us":lease.started_at_us,"template":lease.template.is_some(),"admission":"acquisition_only"}).to_string())
        }));
        match result {Ok(Ok(value))=>value,Ok(Err(reason))=>serde_json::json!({"status":"abstained","reason":reason}).to_string(),Err(_)=>serde_json::json!({"status":"abstained","reason":"native graph preparation failed"}).to_string()}
    })
}

#[pyfunction]
fn optimize_discover_provider_graph_node(
    py:Python<'_>,lease_id:&str,position_json:&str,source_json:&str,observation_only:bool,
)->String {
    py.allow_threads(|| {
        let result=std::panic::catch_unwind(AssertUnwindSafe(||->Result<String,String>{
            let state=optimizer_state();
            let database=state.cost_db.as_ref().ok_or("discovery store is unavailable")?;
            let catalog=state.model_catalog.as_deref().ok_or("discovery catalog is unavailable")?;
            let mut conn=database.lock().map_err(|_|"discovery store is unavailable")?;
            let lease=agentc_optimizer::provider_graph_preparation::load(&conn,lease_id).map_err(|e|e.to_string())?;
            if lease.window_size!=state.plan_profiles.retained_window_size(){return Err("discovery window changed".into());}
            let position=serde_json::from_str(position_json).map_err(|e|e.to_string())?;
            let raw=agentc_optimizer::provider_graph_sampling::plan_discovery(&mut conn,&state.optimizer,catalog,lease_id,position,source_json,observation_only).map_err(|e|e.to_string())?;
            Ok(serde_json::json!({"status":"recorded","plan_json":raw}).to_string())
        }));
        match result {Ok(Ok(value))=>value,Ok(Err(reason))=>serde_json::json!({"status":"abstained","reason":reason}).to_string(),Err(_)=>serde_json::json!({"status":"abstained","reason":"native graph discovery failed"}).to_string()}
    })
}

#[pyfunction]
fn optimize_materialize_provider_graph_node(
    py:Python<'_>,lease_id:&str,position_json:&str,source_json:&str,observation_only:bool,
)->String {
    py.allow_threads(|| {
        let result=std::panic::catch_unwind(AssertUnwindSafe(||->Result<String,String>{
            let state=optimizer_state();let config=state.optimizer.config();
            let policy=agentc_optimizer::provider_graph_quality::Policy {divergence_threshold:config.selection.global_divergence_threshold.unwrap_or(0.0),exposure_budget:config.selection.divergence_exposure_budget,min_pairs:config.selection.min_plan_evidence.max(20) as usize};
            let catalog=state.model_catalog.as_deref().ok_or("template catalog is unavailable")?;
            let database=state.cost_db.as_ref().ok_or("template preparation store is unavailable")?;
            let conn=database.lock().map_err(|_|"template preparation store is unavailable")?;
            let position=serde_json::from_str(position_json).map_err(|e|e.to_string())?;
            let lease=agentc_optimizer::provider_graph_preparation::load(&conn,lease_id).map_err(|e|e.to_string())?;
            agentc_optimizer::provider_graph_deployment::verify_dispatch_policy(&conn,&lease.batch_id,policy,&config.selection_policy(now_us_i64()),state.plan_profiles.retained_window_size()).map_err(|e|e.to_string())?;
            let tx=conn.unchecked_transaction().map_err(|e|e.to_string())?;
            let template=agentc_optimizer::provider_graph_preparation::template_for_dispatch(&tx,lease_id,position,policy,state.plan_profiles.retained_window_size(),now_us_i64()).map_err(|e|e.to_string())?;
            let plan=agentc_optimizer::ffi::materialize_graph_node(&state.optimizer,catalog,&template,position,source_json,observation_only).map_err(|e|e.to_string())?;
            tx.commit().map_err(|e|e.to_string())?;
            Ok(serde_json::json!({"status":"recorded","plan_json":plan}).to_string())
        }));
        match result {Ok(Ok(value))=>value,Ok(Err(reason))=>serde_json::json!({"status":"abstained","reason":reason}).to_string(),Err(_)=>serde_json::json!({"status":"abstained","reason":"native template planning failed"}).to_string()}
    })
}

#[pyfunction]
fn optimize_describe_provider_graph_node(
    py: Python<'_>,
    position_json: &str,
    source_json: &str,
    plan_json: &str,
) -> String {
    py.allow_threads(|| {
        let result = std::panic::catch_unwind(AssertUnwindSafe(|| -> Result<String, String> {
            let state = optimizer_state();
            let catalog = state.model_catalog.as_deref().ok_or("graph preparation catalog is unavailable")?;
            let position = serde_json::from_str(position_json).map_err(|e| e.to_string())?;
            let node = agentc_optimizer::ffi::prepared_graph_node(catalog, position, source_json, plan_json)?;
            Ok(serde_json::json!({"status":"recorded","node":node,"admission":"descriptor_only"}).to_string())
        }));
        match result {
            Ok(Ok(value)) => value,
            Ok(Err(reason)) => serde_json::json!({"status":"abstained","reason":reason}).to_string(),
            Err(_) => serde_json::json!({"status":"abstained","reason":"native graph node description failed"}).to_string(),
        }
    })
}

#[pyfunction(signature = (lease_id, position_json, source_json, plan_json, request_sha256=None, transport_sha256=None))]
fn optimize_approve_provider_graph_node(
    py: Python<'_>,
    lease_id: &str,
    position_json: &str,
    source_json: &str,
    plan_json: &str,
    request_sha256: Option<&str>,
    transport_sha256: Option<&str>,
) -> String {
    py.allow_threads(|| {
        let result=std::panic::catch_unwind(AssertUnwindSafe(||->Result<String,String>{
            let state=optimizer_state();let config=state.optimizer.config();
            let policy=agentc_optimizer::provider_graph_quality::Policy {divergence_threshold:config.selection.global_divergence_threshold.unwrap_or(0.0),exposure_budget:config.selection.divergence_exposure_budget,min_pairs:config.selection.min_plan_evidence.max(20) as usize};
            let catalog=state.model_catalog.as_deref().ok_or("graph preparation catalog is unavailable")?;
            let position=serde_json::from_str(position_json).map_err(|e|e.to_string())?;
            let node=agentc_optimizer::ffi::prepared_graph_node(catalog,position,source_json,plan_json)?;
            let database=state.cost_db.as_ref().ok_or("graph preparation store is unavailable")?;
            let mut conn=database.lock().map_err(|_|"graph preparation store is unavailable")?;
            let lease=agentc_optimizer::provider_graph_preparation::load(&conn,lease_id).map_err(|e|e.to_string())?;
            if lease.deployment_id.is_some() {
                agentc_optimizer::provider_graph_deployment::verify_dispatch_policy(&conn,&lease.batch_id,policy,&config.selection_policy(now_us_i64()),state.plan_profiles.retained_window_size()).map_err(|e|e.to_string())?;
                let binding=agentc_optimizer::provider_graph_deployment::PrimaryBinding {
                    node,request_sha256:request_sha256.ok_or("guarded primary request fingerprint is missing")?.into(),
                    transport_sha256:transport_sha256.ok_or("guarded primary transport fingerprint is missing")?.into(),
                };
                agentc_optimizer::provider_graph_deployment::approve_primary_node(&mut conn,lease_id,&binding,policy,state.plan_profiles.retained_window_size(),now_us_i64()).map_err(|e|e.to_string())?;
            } else {
                agentc_optimizer::provider_graph_preparation::approve_node(&mut conn,lease_id,&node,policy,state.plan_profiles.retained_window_size(),now_us_i64()).map_err(|e|e.to_string())?;
            }
            Ok(serde_json::json!({"status":"recorded"}).to_string())
        }));
        match result {Ok(Ok(value))=>value,Ok(Err(reason))=>serde_json::json!({"status":"abstained","reason":reason}).to_string(),Err(_)=>serde_json::json!({"status":"abstained","reason":"native graph node approval failed"}).to_string()}
    })
}

#[pyfunction]
fn optimize_prepare_provider_batch(py: Python<'_>, batch_json: &str, options_json: &str) -> String {
    py.allow_threads(|| {
        let state = optimizer_state();
        let started = std::time::Instant::now();
        let result = std::panic::catch_unwind(AssertUnwindSafe(|| -> Result<String,String> {
            let request = serde_json::from_str(batch_json).map_err(|error| error.to_string())?;
            let options: agentc_optimizer::prepared_batch::PreparedBatchOptions = serde_json::from_str(options_json).map_err(|error| error.to_string())?;
            if options.max_workers == 0 || options.max_workers > agentc_optimizer::prepared_batch::MAX_PREPARED_BATCH_WORKERS
                || options.remaining_batches == 0 || !matches!(options.mode.as_str(),"guarded"|"reference"|"candidate") {
                return Err("prepared batch options are invalid".into());
            }
            let catalog = state.model_catalog.as_ref().ok_or("prepared batch catalog is unavailable")?;
            let prepared = agentc_optimizer::ffi::prepare_provider_batch(catalog,&state.plan_profiles,request)?;
            let mut workers = if options.mode == "candidate" { options.max_workers.min(prepared.spec.item_count) } else { 1 };
            let mut schedule = serde_json::json!({"max_workers":workers,"selected_reference":workers==1,"reason":format!("explicit_{}",options.mode)});
            if options.mode == "guarded" {
                let config = state.optimizer.config();
                if !options.scope_eligible || !config.enabled {
                    schedule["reason"] = serde_json::json!("optimization_excluded");
                } else if !prepared.catalog_qualified {
                    schedule["reason"] = serde_json::json!("model_catalog_unqualified");
                } else if !prepared.aliases_resolved {
                    schedule["reason"] = serde_json::json!("provider_alias_unresolved");
                } else {
                    let database = state.cost_db.as_ref().ok_or("prepared batch store is unavailable")?;
                    let mut conn = database.lock().map_err(|_| "prepared batch store is unavailable")?;
                    let policy = agentc_optimizer::SelectionPolicy {
                        objective:options.objective,min_paired_observations:config.selection.min_plan_evidence,
                        now_us:now_us_i64(),max_profile_age_us:(config.selection.plan_profile_freshness_hours*3_600_000_000.0) as i64,
                        divergence_exposure_budget:config.selection.divergence_exposure_budget,
                        max_expected_cost_usd:options.max_expected_cost_usd,max_expected_latency_ms:options.max_expected_latency_ms,
                    };
                    let selected = if let Some(tariff) = &options.tariff_policy {
                        let selected=agentc_optimizer::provider_tariff_admission::select(
                            &mut conn,&state.plan_guard,
                            agentc_optimizer::provider_tariff_admission::Request {
                                reference:&prepared.spec,runtime:&prepared.runtime,max_workers:options.max_workers,
                                remaining_batches:options.remaining_batches,window_size:state.plan_profiles.retained_window_size(),
                                policy:&policy,tariff,
                            },
                        ).map_err(|error|error.to_string())?;
                        workers=selected.max_workers;
                        serde_json::to_value(selected).map_err(|error|error.to_string())?
                    } else {
                    let selected = agentc_optimizer::prepared_batch::select_prepared_schedule(
                        &mut conn,&state.plan_profiles,&state.plan_guard,
                        agentc_optimizer::prepared_batch::PreparedScheduleRequest {
                            reference:&prepared.spec,runtime:&prepared.runtime,
                            max_workers:options.max_workers,remaining_batches:options.remaining_batches,policy:&policy,
                        },
                    ).map_err(|error| error.to_string())?;
                    workers = selected.max_workers;
                    serde_json::to_value(selected).map_err(|error| error.to_string())?
                    };
                    schedule = selected;
                    if started.elapsed().as_secs_f64()*1000.0 > config.max_overhead_ms.max(0.0) as f64 {
                        workers = 1;
                        schedule["max_workers"] = serde_json::json!(1);
                        schedule["selected_reference"] = serde_json::json!(true);
                        schedule["selected_key"] = serde_json::to_value(prepared.spec.profile_key().map_err(|error| error.to_string())?).map_err(|error| error.to_string())?;
                        schedule["reason"] = serde_json::json!("planning_overhead_budget");
                    }
                }
            }
            let id=options.provider_receipt_batch_id.as_deref().ok_or("provider economic batch identity is required")?;
            let database=state.cost_db.as_ref().ok_or("provider economic store is unavailable")?;
            let mut conn=database.lock().map_err(|_| "provider economic store is unavailable")?;
            let enrolled=agentc_optimizer::provider_economics::enroll_batch(&mut conn,id,&prepared.spec,
                if options.mode=="guarded" { agentc_optimizer::provider_economics::Phase::Deployment } else { agentc_optimizer::provider_economics::Phase::Acquisition }
            ).map_err(|error|error.to_string())?;
            if !enrolled { return Err("provider economic batch was already enrolled".into()); }
            serde_json::to_string(&serde_json::json!({"status":"prepared","plans":prepared.plans,"max_workers":workers,"schedule":schedule,"provider_receipt_batch_id":id}))
                .map_err(|error| error.to_string())
        }));
        match result {
            Ok(Ok(result)) => result,
            Ok(Err(reason)) => serde_json::json!({"status":"abstained","reason":reason}).to_string(),
            Err(_) => serde_json::json!({"status":"abstained","reason":"prepared batch planning failed"}).to_string(),
        }
    })
}

/// Attach one shadow-mode divergence sample to its exact observed execution.
/// The Python layer treats ``observation_token`` as opaque. Rust validates its
/// complete-plan key, runtime version, and sequence before mutating the plan
/// profile. Solo plans also feed the compatibility rule budget; composed plans
/// deliberately do not fabricate identical causal evidence for every rule.
///
/// The numeric threshold was resolved when the plan was returned and is bound
/// into both its canonical identity and opaque observation token. Precedence at
/// planning is `AGENTC_SHADOW_DIVERGENCE_BUDGET`, then the minimum declared
/// budget among the plan's rules, then 0.05. The minimum is a plan-level policy;
/// it does not propagate a composed outcome into constituent rule evidence.
/// Non-finite and out-of-range divergence is discarded; an invalid threshold
/// falls through to the next source. Never raises — the user-visible call has
/// already returned.
fn unit_fraction_f32(value: f32) -> Option<f32> {
    (value.is_finite() && (0.0..=1.0).contains(&value)).then_some(value)
}

fn unit_fraction_f64(value: f64) -> Option<f32> {
    (value.is_finite() && (0.0..=1.0).contains(&value)).then_some(value as f32)
}

type LegacyDisable = (String, i64, i64);

fn apply_divergence_guards(
    state: &OptimizerState,
    attribution: &DivergenceAttribution,
    divergence: f64,
    now: i64,
) -> Option<LegacyDisable> {
    let legacy_divergence = unit_fraction_f64(divergence)?;
    let plan_guard_outcome = if attribution.newly_recorded && attribution.guard_eligible {
        attribution
            .plan_observation
            .as_ref()
            .zip(attribution.divergence_threshold)
            .and_then(|(observation, plan_threshold)| {
                match state.plan_guard.record_sample(
                    observation,
                    divergence,
                    plan_threshold,
                    Some(now),
                ) {
                    Ok(outcome) => Some(outcome),
                    Err(error) => {
                        eprintln!("[agentc-profiler] plan guard update failed: {error}");
                        None
                    }
                }
            })
    } else {
        None
    };
    if let Some(PlanGuardOutcome::Disabled(disabled)) = plan_guard_outcome {
        eprintln!(
            "[agentc] shadow guard auto-disabled complete plan \
             (exposure={:.3} >= budget={:.3}, cooldown_until={})",
            disabled.exposure,
            state.plan_guard.exposure_budget(),
            disabled.reenable_at_us,
        );
    }

    if !attribution.newly_recorded {
        return None;
    }
    let rule = attribution.solo_rule.as_deref()?;
    let threshold = std::env::var("AGENTC_SHADOW_DIVERGENCE_BUDGET")
        .ok()
        .and_then(|value| value.trim().parse::<f64>().ok())
        .and_then(unit_fraction_f64)
        .or_else(|| {
            state
                .optimizer
                .accuracy_budget_for(rule)
                .and_then(unit_fraction_f32)
        })
        .unwrap_or(0.05);
    let outcome = state.budget.record_sample(
        &attribution.call_site_id,
        rule,
        legacy_divergence,
        threshold,
        now,
    );
    match outcome {
        SampleOutcome::Disable {
            disabled_at_us,
            reenable_at_us,
        } => {
            eprintln!(
                "[agentc] shadow guard auto-disabled rule={rule} site={} \
                 (divergence={legacy_divergence:.3} > budget={threshold:.3})",
                attribution.call_site_id,
            );
            Some((rule.to_string(), disabled_at_us, reenable_at_us))
        }
        _ => None,
    }
}

fn persist_divergence_state(
    state: &OptimizerState,
    connection: &mut Connection,
    attribution: &DivergenceAttribution,
    disable: Option<LegacyDisable>,
) {
    if let Some((rule, disabled_at_us, reenable_at_us)) = disable {
        if let Err(error) = state.budget.persist_disable(
            connection,
            &attribution.call_site_id,
            &rule,
            "shadow_divergence",
            disabled_at_us,
            reenable_at_us,
        ) {
            eprintln!("[agentc-profiler] disable persistence failed: {error}");
        }
    }
    if let Err(error) = state.budget.flush_divergence(connection) {
        eprintln!("[agentc-profiler] divergence flush failed: {error}");
    }
    if let Err(error) = agentc_optimizer::feedback::flush(
        connection,
        &state.plan_profiles,
        &state.plan_guard,
    ) {
        eprintln!("[agentc-profiler] complete-plan feedback flush failed: {error}");
    }
}

#[pyfunction]
fn optimize_record_divergence(py: Python<'_>, observation_token: &str, divergence: f64) {
    py.allow_threads(|| {
        let _ = std::panic::catch_unwind(AssertUnwindSafe(|| {
            if unit_fraction_f64(divergence).is_none() {
                return;
            }
            let state = optimizer_state();
            let Ok(attribution) =
                record_observation_divergence(&state.plan_profiles, observation_token, divergence)
            else {
                return;
            };
            let now = now_us_i64();
            let disable = apply_divergence_guards(&state, &attribution, divergence, now);
            if let Some(mu) = state.cost_db.as_ref() {
                if let Ok(mut conn) = mu.lock() {
                    persist_divergence_state(&state, &mut conn, &attribution, disable);
                }
            }
        }));
    });
}

/// Commit one reference-visible exploration result. The opaque token binds
/// the outcome to the durable lease and exact plan profile. Returns false on
/// any malformed input or persistence failure; never raises into Python.
#[pyfunction]
fn optimize_complete_exploration(
    py: Python<'_>,
    lease_token: &str,
    outcome_json: &str,
    divergence: f64,
) -> bool {
    py.allow_threads(|| {
        std::panic::catch_unwind(AssertUnwindSafe(|| {
            let state = optimizer_state();
            let Some(mutex) = state.cost_db.as_ref() else {
                return false;
            };
            let Ok(mut connection) = mutex.lock() else {
                return false;
            };
            let now = now_us_i64();
            match rust_complete_exploration(
                &state.plan_profiles,
                &state.exploration_controller,
                &mut connection,
                lease_token,
                outcome_json,
                divergence,
                now,
            ) {
                Ok(Some(attribution)) => {
                    let disable = apply_divergence_guards(&state, &attribution, divergence, now);
                    persist_divergence_state(&state, &mut connection, &attribution, disable);
                    true
                }
                Ok(None) => true,
                Err(error) => {
                    eprintln!("[agentc-profiler] exploration completion failed: {error}");
                    false
                }
            }
        }))
        .unwrap_or(false)
    })
}

/// Release a failed exploration lease while retaining its spend attempt in
/// the rolling cap. Returns false on invalid/replayed input and never raises.
#[pyfunction]
fn optimize_fail_exploration(py: Python<'_>, lease_token: &str) -> bool {
    py.allow_threads(|| {
        std::panic::catch_unwind(AssertUnwindSafe(|| {
            let state = optimizer_state();
            let Some(mutex) = state.cost_db.as_ref() else {
                return false;
            };
            let Ok(mut connection) = mutex.lock() else {
                return false;
            };
            match rust_fail_exploration(
                &state.exploration_controller,
                &mut connection,
                lease_token,
                now_us_i64(),
            ) {
                Ok(_) => true,
                Err(error) => {
                    eprintln!("[agentc-profiler] exploration failure update failed: {error}");
                    false
                }
            }
        }))
        .unwrap_or(false)
    })
}

/// Force-flush cost profiles, plan profiles, and guard divergence to
/// `cost_model.db`. Called from Python at process shutdown so final partial
/// state isn't lost. No-op when the optimizer wasn't successfully wired.
#[pyfunction]
fn optimize_flush(py: Python<'_>) {
    py.allow_threads(|| {
        let state = optimizer_state();
        flush_optimizer_state(&state);
    });
}

/// The `_native` Python module.
#[pymodule]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", VERSION)?;
    m.add_function(wrap_pyfunction!(write_span, m)?)?;
    m.add_function(wrap_pyfunction!(create_db, m)?)?;
    m.add_function(wrap_pyfunction!(query_spans_by_trace, m)?)?;
    m.add_function(wrap_pyfunction!(read_trace_content, m)?)?;
    m.add_function(wrap_pyfunction!(merge_all_pending, m)?)?;
    m.add_function(wrap_pyfunction!(cache_lookup, m)?)?;
    m.add_function(wrap_pyfunction!(cache_insert, m)?)?;
    m.add_function(wrap_pyfunction!(cache_invalidate, m)?)?;
    m.add_function(wrap_pyfunction!(cache_stats, m)?)?;
    m.add_function(wrap_pyfunction!(cache_maintenance, m)?)?;
    m.add_function(wrap_pyfunction!(output_content_load, m)?)?;
    m.add_function(wrap_pyfunction!(embed_text_bytes, m)?)?;
    m.add_function(wrap_pyfunction!(canonicalize_prompt_bytes, m)?)?;
    m.add_function(wrap_pyfunction!(canonicalize_parameters_bytes, m)?)?;
    m.add_function(wrap_pyfunction!(optimize_configure, m)?)?;
    m.add_function(wrap_pyfunction!(optimize_configuration, m)?)?;
    m.add_function(wrap_pyfunction!(optimize_evaluation_planner_mode, m)?)?;
    m.add_function(wrap_pyfunction!(optimize_evaluation_policy_version, m)?)?;
    m.add_function(wrap_pyfunction!(
        optimize_provider_configuration_version,
        m
    )?)?;
    m.add_function(wrap_pyfunction!(optimize_application_version, m)?)?;
    m.add_function(wrap_pyfunction!(optimize_output_validation_version, m)?)?;
    m.add_function(wrap_pyfunction!(optimize_divergence_metric_version, m)?)?;
    m.add_function(wrap_pyfunction!(optimize_sdk_response_cache_key, m)?)?;
    m.add_function(wrap_pyfunction!(optimize_storage_path, m)?)?;
    m.add_function(wrap_pyfunction!(optimize_reset, m)?)?;
    m.add_function(wrap_pyfunction!(optimize_plan, m)?)?;
    m.add_function(wrap_pyfunction!(optimize_propose_rule, m)?)?;
    m.add_function(wrap_pyfunction!(
        optimize_text_action_comparator_version,
        m
    )?)?;
    m.add_function(wrap_pyfunction!(optimize_model_catalog, m)?)?;
    m.add_function(wrap_pyfunction!(optimize_observe, m)?)?;
    m.add_function(wrap_pyfunction!(optimize_observe_multi_call, m)?)?;
    m.add_function(wrap_pyfunction!(optimize_record_provider_receipt, m)?)?;
    m.add_function(wrap_pyfunction!(optimize_compare_multi_call, m)?)?;
    m.add_function(wrap_pyfunction!(optimize_compare_provider_graph, m)?)?;
    m.add_function(wrap_pyfunction!(optimize_provider_graph_sampling, m)?)?;
    m.add_function(wrap_pyfunction!(optimize_compare_provider_role_graph, m)?)?;
    m.add_function(wrap_pyfunction!(optimize_begin_provider_graph, m)?)?;
    m.add_function(wrap_pyfunction!(optimize_describe_provider_graph_node, m)?)?;
    m.add_function(wrap_pyfunction!(optimize_approve_provider_graph_node, m)?)?;
    m.add_function(wrap_pyfunction!(optimize_materialize_provider_graph_node, m)?)?;
    m.add_function(wrap_pyfunction!(optimize_discover_provider_graph_node, m)?)?;
    m.add_function(wrap_pyfunction!(optimize_original_provider_call, m)?)?;
    m.add_function(wrap_pyfunction!(optimize_prepare_provider_batch, m)?)?;
    m.add_function(wrap_pyfunction!(optimize_record_divergence, m)?)?;
    m.add_function(wrap_pyfunction!(
        optimize_record_output_validation_rejection,
        m
    )?)?;
    m.add_function(wrap_pyfunction!(optimize_complete_exploration, m)?)?;
    m.add_function(wrap_pyfunction!(optimize_fail_exploration, m)?)?;
    m.add_function(wrap_pyfunction!(optimize_flush, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_required_keys_list() {
        assert_eq!(
            REQUIRED_KEYS,
            &["span_id", "trace_id", "name", "kind", "start_time"]
        );
    }

    #[test]
    fn test_version_matches_cargo_pkg() {
        assert_eq!(VERSION, env!("CARGO_PKG_VERSION"));
    }

    #[test]
    fn guard_fractions_reject_non_finite_and_out_of_range_values() {
        for invalid in [
            f64::NAN,
            f64::INFINITY,
            f64::NEG_INFINITY,
            -f64::MIN_POSITIVE,
            1.0 + f64::EPSILON,
            -0.1,
            1.1,
        ] {
            assert!(unit_fraction_f64(invalid).is_none());
        }
        for valid in [0.0, 0.5, 1.0] {
            assert_eq!(unit_fraction_f64(valid), Some(valid as f32));
        }
        for invalid in [f32::NAN, f32::INFINITY, f32::NEG_INFINITY, -0.1, 1.1] {
            assert!(unit_fraction_f32(invalid).is_none());
        }
    }

    #[test]
    fn plan_guard_panic_falls_back_to_reference() {
        let plan = guard_plan_fail_safe(|| panic!("injected plan-guard failure"));
        assert_eq!(plan, PASS_THROUGH_JSON);
    }
}

// Python-dependent tests live in tests/test_native.py (run via `maturin develop && pytest`).
// PyO3 cdylib crates cannot link against Python for `cargo test`.
