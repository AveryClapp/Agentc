//! PyO3 bindings for the Agentc profiler.
//!
//! Exposes `_native` Python module with `write_span()` as the primary FFI entry point.
//! All heavy lifting (hashing, compression, embedding, SQLite writes) happens on the Rust
//! side. The Python layer is as thin as possible.

#![allow(clippy::useless_conversion)] // PyO3 macro-generated code triggers this

use std::panic::AssertUnwindSafe;
use std::path::Path;
use std::sync::{Arc, Mutex, OnceLock};

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
    ffi::{optimize_observe as rust_observe, optimize_plan as rust_plan, PASS_THROUGH_JSON},
    planner::{Optimizer, Plan},
    Budget, Wired,
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
    let attrs_parsed: serde_json::Value =
        serde_json::from_str(&attributes_json).unwrap_or(serde_json::Value::Object(Default::default()));

    let model = opt_str(dict, "model")?.or_else(|| attrs_str(&attrs_parsed, "gen_ai.response.model"))
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
        let mut guard = state()
            .lock()
            .map_err(|e| PyRuntimeError::new_err(format!("write_span: state lock poisoned: {e}")))?;
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
fn query_spans_by_trace(
    py: Python<'_>,
    db_path: &str,
    trace_id: &str,
) -> PyResult<Py<PyList>> {
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
fn span_to_pydict<'py>(py: Python<'py>, span: &agentc_core::span::Span) -> PyResult<Bound<'py, PyDict>> {
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
        let mut guard = state()
            .lock()
            .map_err(|e| PyRuntimeError::new_err(format!("cache_insert: state lock poisoned: {e}")))?;
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
        let guard = state().lock().ok()?;
        let conn = guard.conn.as_ref()?;
        conn.query_row(
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
fn embed_text_bytes<'py>(
    py: Python<'py>,
    text: &str,
) -> PyResult<Option<Bound<'py, PyBytes>>> {
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

/// Process-global optimizer. Lazily constructed on first FFI call. The
/// state holds:
///
/// - A fully-wired `Optimizer` with all five rewrite rules.
/// - The `CostModel` and `Budget` warmed from `cost_model.db` on init.
/// - A `Mutex<Connection>` for `optimizer_audit.db`. We write `plan_audit`
///   rows synchronously from the hot path; SQLite WAL mode keeps the
///   per-row latency well under a millisecond.
/// - An `AtomicU64` observe counter so we can periodically flush the
///   cost-model `dirty` set without spawning a thread.
///
/// On any wiring failure (e.g. corrupted `cost_model.db`) we fall back to
/// an empty optimizer — the user's LLM call must never break because the
/// optimizer itself failed to initialize.
struct OptimizerState {
    optimizer: Arc<Optimizer>,
    cost_model: Arc<CostModel>,
    #[allow(dead_code)]
    budget: Arc<Budget>,
    audit: Option<Mutex<Connection>>,
    cost_db: Option<Mutex<Connection>>,
    observe_counter: std::sync::atomic::AtomicU64,
}

static OPTIMIZER: OnceLock<OptimizerState> = OnceLock::new();

/// Flush the cost model to `cost_model.db` every N observes. 16 is a
/// compromise between losing too many in-flight samples on a crash and
/// hammering SQLite for every observe; bench runs do ~100 observes, so 16
/// → ~6 flushes per run.
const COST_MODEL_FLUSH_EVERY: u64 = 16;

fn resolve_storage_dir() -> std::path::PathBuf {
    agentc_core::merge::agentc_data_dir()
}

fn optimizer_state() -> &'static OptimizerState {
    OPTIMIZER.get_or_init(|| {
        let config = OptimizerConfig::from_env();
        let storage = resolve_storage_dir();

        match build_optimizer(&storage, config.clone()) {
            Ok(Wired { optimizer, cost_model, budget, audit_conn }) => {
                // Reopen cost DB for periodic flush — the `Wired::audit_conn`
                // is for the audit table; `cost_model.db` is a separate file.
                let cost_db = Connection::open(storage.join("cost_model.db"))
                    .ok()
                    .map(Mutex::new);
                OptimizerState {
                    optimizer,
                    cost_model,
                    budget,
                    audit: Some(Mutex::new(audit_conn)),
                    cost_db,
                    observe_counter: std::sync::atomic::AtomicU64::new(0),
                }
            }
            Err(e) => {
                eprintln!("[agentc-profiler] optimizer wiring failed: {e}");
                let cost_model = Arc::new(CostModel::new());
                let optimizer = Arc::new(Optimizer::empty(cost_model.clone(), config));
                OptimizerState {
                    optimizer,
                    cost_model,
                    budget: Arc::new(Budget::new()),
                    audit: None,
                    cost_db: None,
                    observe_counter: std::sync::atomic::AtomicU64::new(0),
                }
            }
        }
    })
}

fn now_us_i64() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_micros() as i64)
        .unwrap_or(0)
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
        let plan_json = std::panic::catch_unwind(AssertUnwindSafe(|| {
            rust_plan(&state.optimizer, call_json)
        }))
        .unwrap_or_else(|_| PASS_THROUGH_JSON.to_string());
        let overhead_us = t0.elapsed().as_micros() as i64;

        // Best-effort audit. The plan is always returned regardless of
        // whether the audit row lands.
        let _ = std::panic::catch_unwind(AssertUnwindSafe(|| {
            write_plan_audit(state, call_json, &plan_json, overhead_us);
        }));

        plan_json
    })
}

/// Decode just enough of the call/plan to log one audit row. Anything
/// missing is treated as "pass-through, no rule" — we'd rather log a
/// partial truth than skip the row entirely.
fn write_plan_audit(state: &OptimizerState, call_json: &str, plan_json: &str, overhead_us: i64) {
    let Some(audit_mu) = state.audit.as_ref() else { return; };

    // Pull call_site_id + span_id from the call. If the call is malformed
    // we already returned PassThrough; record it as such with empty IDs so
    // the audit row count still tracks invocations.
    let (call_site_id, span_id) = match serde_json::from_str::<serde_json::Value>(call_json) {
        Ok(v) => {
            let site = v.get("call_site_id")
                .and_then(|x| x.as_str())
                .unwrap_or("")
                .to_string();
            let span = v.get("span_id")
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
        Plan::Rewritten { rule, projected_savings_usd, .. } => (
            PlanKind::Rewritten,
            Some(rule.clone()),
            Some(*projected_savings_usd as f64),
        ),
        Plan::Parallel { rule, projected_savings_usd, .. } => (
            PlanKind::Parallel,
            Some(rule.clone()),
            Some(*projected_savings_usd as f64),
        ),
        Plan::Composed { rules, net_savings_usd, .. } => (
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

    let Ok(conn) = audit_mu.lock() else { return; };
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
fn optimize_observe(py: Python<'_>, plan_json: &str, outcome_json: &str) {
    py.allow_threads(|| {
        let state = optimizer_state();
        let _ = std::panic::catch_unwind(AssertUnwindSafe(|| {
            let _ = rust_observe(&state.cost_model, plan_json, outcome_json);
        }));

        let n = state
            .observe_counter
            .fetch_add(1, std::sync::atomic::Ordering::Relaxed)
            + 1;
        if n % COST_MODEL_FLUSH_EVERY == 0 {
            if let Some(mu) = state.cost_db.as_ref() {
                let _ = std::panic::catch_unwind(AssertUnwindSafe(|| {
                    if let Ok(mut conn) = mu.lock() {
                        if let Err(e) = state.cost_model.flush_dirty(&mut conn) {
                            eprintln!("[agentc-profiler] cost_model flush failed: {e}");
                        }
                    }
                }));
            }
        }
    });
}

/// Force-flush the cost model to `cost_model.db`. Called from Python at
/// process shutdown so the final partial batch isn't lost. No-op when the
/// optimizer wasn't successfully wired.
#[pyfunction]
fn optimize_flush(py: Python<'_>) {
    py.allow_threads(|| {
        let state = optimizer_state();
        if let Some(mu) = state.cost_db.as_ref() {
            let _ = std::panic::catch_unwind(AssertUnwindSafe(|| {
                if let Ok(mut conn) = mu.lock() {
                    let _ = state.cost_model.flush_dirty(&mut conn);
                }
            }));
        }
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
    m.add_function(wrap_pyfunction!(optimize_plan, m)?)?;
    m.add_function(wrap_pyfunction!(optimize_observe, m)?)?;
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
}

// Python-dependent tests live in tests/test_native.py (run via `maturin develop && pytest`).
// PyO3 cdylib crates cannot link against Python for `cargo test`.
