//! PyO3 bindings for the Agentc profiler.
//!
//! Exposes `_native` Python module with `write_span()` as the primary FFI entry point.
//! All heavy lifting (hashing, compression, embedding, SQLite writes) happens on the Rust
//! side. The Python layer is as thin as possible.

#![allow(clippy::useless_conversion)] // PyO3 macro-generated code triggers this

use std::path::Path;
use std::sync::{Mutex, OnceLock};

use pyo3::exceptions::{PyRuntimeError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use rusqlite::Connection;

use agentc_core::storage::{self, SpanInput, WriteSpanOptions};

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

/// The `_native` Python module.
#[pymodule]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", VERSION)?;
    m.add_function(wrap_pyfunction!(write_span, m)?)?;
    m.add_function(wrap_pyfunction!(create_db, m)?)?;
    m.add_function(wrap_pyfunction!(query_spans_by_trace, m)?)?;
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
