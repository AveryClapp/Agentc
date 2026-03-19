//! PyO3 bindings for the Agentc profiler.
//!
//! Exposes `_native` Python module with `write_span()` as the primary FFI entry point.
//! All heavy lifting (hashing, compression, embedding, SQLite writes) happens on the Rust
//! side. The Python layer is as thin as possible.

#![allow(clippy::useless_conversion)] // PyO3 macro-generated code triggers this

use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyDict;

/// Package version, exposed as `agentc._native.__version__`.
const VERSION: &str = env!("CARGO_PKG_VERSION");

/// Required keys that must be present in every span dict.
const REQUIRED_KEYS: &[&str] = &["span_id", "trace_id", "name", "kind", "start_time"];

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

/// Write a span dict from Python into the native storage layer.
///
/// The dict must contain at minimum: `span_id`, `trace_id`, `name`, `kind`, `start_time`.
/// Optional keys: `parent_span_id`, `end_time`, `status`, `model`, `provider`,
/// `input_tokens`, `output_tokens`, `cache_creation_tokens`, `cache_read_tokens`,
/// `attributes`, `input_messages`, `output_messages`.
///
/// In later beads this function will compute content hashes, zstd compression,
/// embeddings, and write to SQLite. Currently validates keys and returns.
#[pyfunction]
fn write_span(py: Python<'_>, span_dict: &Bound<'_, PyAny>) -> PyResult<()> {
    let dict = span_dict
        .downcast::<PyDict>()
        .map_err(|_| PyTypeError::new_err("write_span: expected a dict argument"))?;

    validate_span_dict(dict)?;

    // Release the GIL for any CPU-bound work.
    // Currently a no-op stub — later beads add hashing, compression, embedding, SQLite write.
    py.allow_threads(|| {
        // TODO: Span ingestion (bd-2db, bd-7e9, bd-1h7)
    });

    Ok(())
}

/// Create or open a SQLite database at the given path.
///
/// Creates the schema (spans, input_content, output_content tables) if the DB is new.
/// The `is_canonical` parameter controls whether the `traces` VIEW is created
/// (only in canonical DB, not per-process DBs).
///
/// TODO: Full implementation in bd-2db.
#[pyfunction]
fn create_db(_path: &str, _is_canonical: bool) -> PyResult<()> {
    Ok(())
}

/// Query all spans for a given trace_id from a SQLite database.
///
/// Returns a list of dicts, each representing a span with all stored fields.
///
/// TODO: Full implementation in bd-2db.
#[pyfunction]
fn query_spans_by_trace(
    py: Python<'_>,
    _db_path: &str,
    _trace_id: &str,
) -> PyResult<Py<pyo3::types::PyList>> {
    // Return empty list for now
    Ok(pyo3::types::PyList::empty_bound(py).unbind())
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
    fn test_version_is_set() {
        assert!(!VERSION.is_empty());
        // Should match workspace version
        assert_eq!(VERSION, "0.1.0");
    }
}

// Python-dependent tests live in tests/test_native.py (run via `maturin develop && pytest`).
// PyO3 cdylib crates cannot link against Python for `cargo test`.
