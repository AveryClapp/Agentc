# Wire up write_span() FFI to SQLite Storage — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Connect the PyO3 profiler bindings (`write_span`, `create_db`, `query_spans_by_trace`) to the existing Rust storage layer so Python spans actually persist to SQLite.

**Architecture:** The Rust storage layer (`agentc-core`) already implements the full write_span flow (canonical JSON, SHA-256 hashing, zstd compression, embedding, SQLite insert). The profiler crate (`agentc-profiler`) just needs to extract Python dict fields into a `SpanInput` struct, manage a per-thread SQLite connection, and delegate to `storage::write_span()`. Similarly, `create_db` and `query_spans_by_trace` are thin wrappers around existing `db::create_db()` and `db::query_spans_by_trace()`.

**Tech Stack:** Rust, PyO3 0.22, rusqlite, serde_json, agentc-core (storage, db, span modules)

**Key files:**
- `crates/agentc-profiler/src/lib.rs` — the file we're modifying
- `crates/agentc-core/src/storage.rs` — `SpanInput`, `WriteSpanOptions`, `write_span()`
- `crates/agentc-core/src/db.rs` — `create_db()`, `query_spans_by_trace()`, `Span`
- `crates/agentc-core/src/span.rs` — `Span` struct definition
- `tests/test_native.py` — Python-side integration tests

**Beads:** bd-2os.1

---

### Task 1: Implement `create_db()` FFI

**Files:**
- Modify: `crates/agentc-profiler/src/lib.rs:64-67`
- Test: `tests/test_native.py`

**Step 1: Write the failing Python test**

Add to `tests/test_native.py` in `TestCreateDb`:

```python
class TestCreateDb:
    def test_creates_database_file(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        _native.create_db(str(db_path), False)
        assert db_path.exists()

    def test_creates_canonical_with_traces_view(self, tmp_path: Path) -> None:
        db_path = tmp_path / "canonical.db"
        _native.create_db(str(db_path), True)
        assert db_path.exists()
        # Canonical DB should have traces VIEW and model_pricing table
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        ).fetchall()}
        conn.close()
        assert "spans" in tables
        assert "input_content" in tables
        assert "output_content" in tables
        assert "traces" in tables
        assert "model_pricing" in tables

    def test_per_process_no_traces_view(self, tmp_path: Path) -> None:
        db_path = tmp_path / "per_process.db"
        _native.create_db(str(db_path), False)
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        ).fetchall()}
        conn.close()
        assert "spans" in tables
        assert "traces" not in tables

    def test_idempotent(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        _native.create_db(str(db_path), False)
        _native.create_db(str(db_path), False)  # Should not raise
```

Add `from pathlib import Path` to test imports if not present.

**Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=python .venv/bin/pytest tests/test_native.py::TestCreateDb -v`
Expected: `test_creates_database_file` FAILS (no file created since create_db is a no-op)

**Step 3: Implement `create_db` in Rust**

In `crates/agentc-profiler/src/lib.rs`, replace the `create_db` function:

```rust
#[pyfunction]
fn create_db(path: &str, is_canonical: bool) -> PyResult<()> {
    let db_path = std::path::Path::new(path);
    agentc_core::db::create_db(db_path, is_canonical)
        .map_err(|e| PyValueError::new_err(format!("create_db failed: {e}")))?;
    Ok(())
}
```

**Step 4: Build and run tests**

Run: `source .venv/bin/activate && maturin develop && PYTHONPATH=python .venv/bin/pytest tests/test_native.py::TestCreateDb -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add crates/agentc-profiler/src/lib.rs tests/test_native.py
git commit -m "Wire create_db FFI to agentc_core::db::create_db"
```

---

### Task 2: Implement `write_span()` FFI — dict extraction + storage call

This is the core task. Extract Python dict fields into a `SpanInput`, open (or reuse) a DB connection, and call `storage::write_span()`.

**Files:**
- Modify: `crates/agentc-profiler/src/lib.rs`
- Test: `tests/test_native.py`

**Step 1: Write the failing Python test**

Add to `tests/test_native.py`:

```python
class TestWriteSpanStorage:
    """Tests that write_span actually persists data to SQLite."""

    def test_span_persisted_to_db(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        _native.create_db(str(db_path), False)
        _native.set_db_path(str(db_path))

        d = _valid_span_dict()
        d["model"] = "claude-sonnet-4"
        d["provider"] = "anthropic"
        d["input_tokens"] = 100
        d["output_tokens"] = 50
        _native.write_span(d)

        import sqlite3
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute("SELECT span_id, trace_id, name, kind FROM spans").fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0][0] == "abc1234567890123"
        assert rows[0][2] == "test-span"
        assert rows[0][3] == "chat"

    def test_content_stored_and_deduped(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        _native.create_db(str(db_path), False)
        _native.set_db_path(str(db_path))

        d = _valid_span_dict()
        d["input_messages"] = [{"role": "user", "content": "hello"}]
        d["output_messages"] = [{"role": "assistant", "content": "hi"}]
        _native.write_span(d)

        import sqlite3
        conn = sqlite3.connect(str(db_path))
        input_rows = conn.execute("SELECT COUNT(*) FROM input_content").fetchone()
        output_rows = conn.execute("SELECT COUNT(*) FROM output_content").fetchone()
        span_row = conn.execute("SELECT input_content_id, output_content_id FROM spans").fetchone()
        conn.close()
        assert input_rows[0] == 1
        assert output_rows[0] == 1
        assert span_row[0] is not None  # input_content_id set
        assert span_row[1] is not None  # output_content_id set

    def test_write_without_db_path_raises(self) -> None:
        _native.set_db_path("")  # Clear
        d = _valid_span_dict()
        with pytest.raises(ValueError, match="no database path configured"):
            _native.write_span(d)

    def test_optional_fields_stored(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        _native.create_db(str(db_path), False)
        _native.set_db_path(str(db_path))

        d = _valid_span_dict()
        d["parent_span_id"] = "parent123"
        d["end_time"] = 1234567891000000
        d["status"] = "ERROR"
        d["model"] = "gpt-4o"
        d["provider"] = "openai"
        d["input_tokens"] = 500
        d["output_tokens"] = 200
        d["cache_creation_tokens"] = 100
        d["cache_read_tokens"] = 50
        d["attributes"] = '{"custom": "attr"}'
        _native.write_span(d)

        import sqlite3
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT parent_span_id, end_time, status, model, provider, "
            "input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens, attributes "
            "FROM spans"
        ).fetchone()
        conn.close()
        assert row[0] == "parent123"
        assert row[1] == 1234567891000000
        assert row[2] == "ERROR"
        assert row[3] == "gpt-4o"
        assert row[4] == "openai"
        assert row[5] == 500
        assert row[6] == 200
        assert row[7] == 100
        assert row[8] == 50
        assert row[9] == '{"custom": "attr"}'
```

**Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=python .venv/bin/pytest tests/test_native.py::TestWriteSpanStorage -v`
Expected: FAIL — `set_db_path` doesn't exist yet

**Step 3: Implement write_span and set_db_path in Rust**

Replace the full `lib.rs` content. Key design decisions:
- Thread-local `RefCell<Option<Connection>>` for the SQLite connection (one connection per thread, no mutex contention)
- `set_db_path()` function to configure where spans are written (called by Python lifecycle during init)
- `write_span()` extracts dict fields into `SpanInput`, opens connection lazily, calls `storage::write_span()`
- `WriteSpanOptions` reads from env vars `AGENTC_CAPTURE_CONTENT` and `AGENTC_CAPTURE_EMBEDDINGS`

```rust
//! PyO3 bindings for the Agentc profiler.

#![allow(clippy::useless_conversion)]

use std::cell::RefCell;
use std::path::PathBuf;
use std::sync::Mutex;

use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

use agentc_core::storage::{SpanInput, WriteSpanOptions};

const VERSION: &str = env!("CARGO_PKG_VERSION");

const REQUIRED_KEYS: &[&str] = &["span_id", "trace_id", "name", "kind", "start_time"];

/// Global database path, set via set_db_path().
static DB_PATH: Mutex<Option<PathBuf>> = Mutex::new(None);

/// Thread-local SQLite connection (lazy-opened on first write).
thread_local! {
    static CONN: RefCell<Option<rusqlite::Connection>> = const { RefCell::new(None) };
}

fn get_db_path() -> PyResult<PathBuf> {
    let guard = DB_PATH.lock().map_err(|e| PyValueError::new_err(format!("lock error: {e}")))?;
    guard
        .clone()
        .ok_or_else(|| PyValueError::new_err("write_span: no database path configured (call set_db_path first)"))
}

fn with_connection<F, R>(f: F) -> PyResult<R>
where
    F: FnOnce(&rusqlite::Connection) -> PyResult<R>,
{
    CONN.with(|cell| {
        let mut borrow = cell.borrow_mut();
        if borrow.is_none() {
            let path = get_db_path()?;
            let conn = agentc_core::db::open_db(&path)
                .map_err(|e| PyValueError::new_err(format!("failed to open db: {e}")))?;
            *borrow = Some(conn);
        }
        f(borrow.as_ref().unwrap())
    })
}

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

/// Helper to extract an optional string from a Python dict.
fn get_opt_str(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<Option<String>> {
    match dict.get_item(key)? {
        Some(val) if val.is_none() => Ok(None),
        Some(val) => Ok(Some(val.extract::<String>()?)),
        None => Ok(None),
    }
}

/// Helper to extract an optional i64 from a Python dict.
fn get_opt_i64(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<Option<i64>> {
    match dict.get_item(key)? {
        Some(val) if val.is_none() => Ok(None),
        Some(val) => Ok(Some(val.extract::<i64>()?)),
        None => Ok(None),
    }
}

/// Convert a Python object (list/dict/str) to serde_json::Value.
fn py_to_json(obj: &Bound<'_, PyAny>) -> PyResult<serde_json::Value> {
    // If it's already a string, parse it as JSON
    if let Ok(s) = obj.extract::<String>() {
        return serde_json::from_str(&s)
            .map_err(|e| PyValueError::new_err(format!("invalid JSON string: {e}")));
    }
    // Otherwise, serialize via Python repr -> JSON
    let json_mod = obj.py().import("json")?;
    let json_str: String = json_mod.call_method1("dumps", (obj,))?.extract()?;
    serde_json::from_str(&json_str)
        .map_err(|e| PyValueError::new_err(format!("JSON conversion failed: {e}")))
}

/// Extract optional messages field (can be a JSON string or a Python list/dict).
fn get_opt_messages(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<Option<serde_json::Value>> {
    match dict.get_item(key)? {
        Some(val) if val.is_none() => Ok(None),
        Some(val) => Ok(Some(py_to_json(&val)?)),
        None => Ok(None),
    }
}

/// Set the database path for write_span to use.
///
/// Called by Python lifecycle (agentc.init) after creating the per-process DB.
/// Pass an empty string to clear.
#[pyfunction]
fn set_db_path(path: &str) -> PyResult<()> {
    let mut guard = DB_PATH.lock().map_err(|e| PyValueError::new_err(format!("lock error: {e}")))?;
    if path.is_empty() {
        *guard = None;
        // Close any open thread-local connections
        CONN.with(|cell| { *cell.borrow_mut() = None; });
    } else {
        *guard = Some(PathBuf::from(path));
        // Force re-open on next write (new path)
        CONN.with(|cell| { *cell.borrow_mut() = None; });
    }
    Ok(())
}

#[pyfunction]
fn write_span(py: Python<'_>, span_dict: &Bound<'_, PyAny>) -> PyResult<()> {
    let dict = span_dict
        .downcast::<PyDict>()
        .map_err(|_| PyTypeError::new_err("write_span: expected a dict argument"))?;

    validate_span_dict(dict)?;

    // Extract all fields from the Python dict.
    let input = SpanInput {
        span_id: dict.get_item("span_id")?.unwrap().extract::<String>()?,
        trace_id: dict.get_item("trace_id")?.unwrap().extract::<String>()?,
        parent_span_id: get_opt_str(dict, "parent_span_id")?,
        name: dict.get_item("name")?.unwrap().extract::<String>()?,
        kind: dict.get_item("kind")?.unwrap().extract::<String>()?,
        start_time: dict.get_item("start_time")?.unwrap().extract::<i64>()?,
        end_time: get_opt_i64(dict, "end_time")?,
        status: get_opt_str(dict, "status")?.unwrap_or_else(|| "OK".to_string()),
        model: get_opt_str(dict, "model")?,
        provider: get_opt_str(dict, "provider")?,
        input_tokens: get_opt_i64(dict, "input_tokens")?,
        output_tokens: get_opt_i64(dict, "output_tokens")?,
        cache_creation_tokens: get_opt_i64(dict, "cache_creation_tokens")?,
        cache_read_tokens: get_opt_i64(dict, "cache_read_tokens")?,
        attributes: get_opt_str(dict, "attributes")?.unwrap_or_else(|| "{}".to_string()),
        input_messages: get_opt_messages(dict, "input_messages")?,
        output_messages: get_opt_messages(dict, "output_messages")?,
    };

    let opts = WriteSpanOptions {
        capture_content: std::env::var("AGENTC_CAPTURE_CONTENT")
            .map(|v| v != "0" && v.to_lowercase() != "false")
            .unwrap_or(true),
        capture_embeddings: std::env::var("AGENTC_CAPTURE_EMBEDDINGS")
            .map(|v| v != "0" && v.to_lowercase() != "false")
            .unwrap_or(true),
    };

    // Release the GIL for CPU-bound Rust work (hashing, compression, SQLite).
    py.allow_threads(|| {
        with_connection(|conn| {
            agentc_core::storage::write_span(conn, &input, opts)
                .map_err(|e| PyValueError::new_err(format!("write_span failed: {e}")))?;
            Ok(())
        })
    })
}

#[pyfunction]
fn create_db(path: &str, is_canonical: bool) -> PyResult<()> {
    let db_path = std::path::Path::new(path);
    agentc_core::db::create_db(db_path, is_canonical)
        .map_err(|e| PyValueError::new_err(format!("create_db failed: {e}")))?;
    Ok(())
}

#[pyfunction]
fn query_spans_by_trace(
    py: Python<'_>,
    db_path: &str,
    trace_id: &str,
) -> PyResult<Py<PyList>> {
    let path = std::path::Path::new(db_path);
    let conn = agentc_core::db::open_db(path)
        .map_err(|e| PyValueError::new_err(format!("open_db failed: {e}")))?;
    let spans = agentc_core::db::query_spans_by_trace(&conn, trace_id)
        .map_err(|e| PyValueError::new_err(format!("query failed: {e}")))?;

    let list = PyList::empty(py);
    for span in spans {
        let dict = PyDict::new(py);
        dict.set_item("span_id", &span.span_id)?;
        dict.set_item("trace_id", &span.trace_id)?;
        dict.set_item("parent_span_id", &span.parent_span_id)?;
        dict.set_item("name", &span.name)?;
        dict.set_item("kind", &span.kind)?;
        dict.set_item("start_time", span.start_time)?;
        dict.set_item("end_time", span.end_time)?;
        dict.set_item("status", &span.status)?;
        dict.set_item("model", &span.model)?;
        dict.set_item("provider", &span.provider)?;
        dict.set_item("input_tokens", span.input_tokens)?;
        dict.set_item("output_tokens", span.output_tokens)?;
        dict.set_item("cache_creation_tokens", span.cache_creation_tokens)?;
        dict.set_item("cache_read_tokens", span.cache_read_tokens)?;
        dict.set_item("cost_usd", span.cost_usd)?;
        dict.set_item("attributes", &span.attributes)?;
        dict.set_item("input_content_id", &span.input_content_id)?;
        dict.set_item("output_content_id", &span.output_content_id)?;
        list.append(dict)?;
    }

    Ok(list.unbind())
}

#[pymodule]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", VERSION)?;
    m.add_function(wrap_pyfunction!(write_span, m)?)?;
    m.add_function(wrap_pyfunction!(create_db, m)?)?;
    m.add_function(wrap_pyfunction!(query_spans_by_trace, m)?)?;
    m.add_function(wrap_pyfunction!(set_db_path, m)?)?;
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
        assert_eq!(VERSION, "0.1.0");
    }
}
```

Also add `rusqlite` to `crates/agentc-profiler/Cargo.toml` dependencies:

```toml
[dependencies]
agentc-core = { workspace = true }
pyo3 = { workspace = true }
serde_json = { workspace = true }
rusqlite = { workspace = true }
```

**Step 4: Build and run tests**

Run: `source .venv/bin/activate && maturin develop && PYTHONPATH=python .venv/bin/pytest tests/test_native.py -v`
Expected: All PASS (both old tests and new tests)

**Step 5: Commit**

```bash
git add crates/agentc-profiler/src/lib.rs crates/agentc-profiler/Cargo.toml tests/test_native.py
git commit -m "Wire write_span FFI to storage layer with dict extraction"
```

---

### Task 3: Wire Python lifecycle to call `set_db_path()`

The Python `init()` in `_lifecycle.py` already calls `create_db()`, but it doesn't tell Rust *where* to write. We need to call `set_db_path()` after `create_db()`.

**Files:**
- Modify: `python/agentc/_lifecycle.py:76-81`
- Modify: `python/agentc/_native.pyi` (add set_db_path stub)
- Test: `tests/test_lifecycle.py`

**Step 1: Write the failing test**

Add to `tests/test_lifecycle.py`:

```python
def test_init_sets_db_path(tmp_path: Path) -> None:
    """After init, write_span should persist to the per-process DB."""
    import agentc
    from agentc._lifecycle import _initialized, _shutdown_in_progress
    from unittest.mock import patch

    storage = tmp_path / "agentc"

    with patch("agentc._lifecycle._apply_patches"):
        agentc.init(storage_path=str(storage))

    # write_span should work (not raise "no database path")
    from agentc._native import write_span
    write_span({
        "span_id": "test-span-001",
        "trace_id": "test-trace-001",
        "name": "test",
        "kind": "chat",
        "start_time": 1000000,
    })

    # Verify the span landed in the per-process DB
    import os, sqlite3
    pid = os.getpid()
    db_path = storage / "active" / f"pid-{pid}.db"
    assert db_path.exists()
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute("SELECT span_id FROM spans").fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0][0] == "test-span-001"

    agentc.shutdown()
    _initialized.clear()
    _shutdown_in_progress.clear()
```

**Step 2: Run test to verify it fails**

Run: `PYTHONPATH=python .venv/bin/pytest tests/test_lifecycle.py::test_init_sets_db_path -v`
Expected: FAIL — write_span raises "no database path configured"

**Step 3: Modify _lifecycle.py to call set_db_path**

In `python/agentc/_lifecycle.py`, after the `create_db` call (around line 81), add:

```python
        from agentc._native import create_db, set_db_path

        create_db(str(db_path), False)  # per-process DB, no traces VIEW
        set_db_path(str(db_path))
```

Also add `set_db_path` to `python/agentc/_native.pyi`:

```python
def set_db_path(path: str) -> None:
    """Set the database path for write_span to use.

    Called during init() after creating the per-process DB.
    Pass empty string to clear.
    """
    ...
```

And in `shutdown()`, clear the db path. In `_lifecycle.py` `shutdown()` function, add after `_flush_queue`:

```python
        from agentc._native import set_db_path
        set_db_path("")  # Clear so stale connections don't linger
```

**Step 4: Build and run tests**

Run: `source .venv/bin/activate && maturin develop && PYTHONPATH=python .venv/bin/pytest tests/test_lifecycle.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add python/agentc/_lifecycle.py python/agentc/_native.pyi tests/test_lifecycle.py
git commit -m "Wire lifecycle init/shutdown to set_db_path for span persistence"
```

---

### Task 4: Implement `query_spans_by_trace()` FFI — end-to-end test

This was already implemented in Task 2's lib.rs rewrite. We just need a proper integration test.

**Files:**
- Test: `tests/test_native.py`

**Step 1: Write the integration test**

Add to `tests/test_native.py`:

```python
class TestQuerySpansByTrace:
    def test_roundtrip_write_then_query(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        _native.create_db(str(db_path), False)
        _native.set_db_path(str(db_path))

        # Write 3 spans across 2 traces
        for i in range(3):
            d = {
                "span_id": f"span-{i}",
                "trace_id": "trace-A" if i < 2 else "trace-B",
                "name": f"call-{i}",
                "kind": "chat",
                "start_time": 1000000 + i * 1000,
                "model": "claude-sonnet-4",
                "input_tokens": 100,
                "output_tokens": 50,
            }
            _native.write_span(d)

        # Query trace-A (should get 2 spans)
        results = _native.query_spans_by_trace(str(db_path), "trace-A")
        assert len(results) == 2
        assert results[0]["span_id"] == "span-0"
        assert results[1]["span_id"] == "span-1"

        # Query trace-B (should get 1 span)
        results = _native.query_spans_by_trace(str(db_path), "trace-B")
        assert len(results) == 1
        assert results[0]["span_id"] == "span-2"

        # Query non-existent trace (should get empty list)
        results = _native.query_spans_by_trace(str(db_path), "trace-Z")
        assert len(results) == 0

    def test_query_returns_all_fields(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        _native.create_db(str(db_path), False)
        _native.set_db_path(str(db_path))

        d = _valid_span_dict()
        d["model"] = "gpt-4o"
        d["provider"] = "openai"
        d["input_tokens"] = 500
        d["output_tokens"] = 200
        d["end_time"] = 1234567891000000
        d["status"] = "OK"
        d["input_messages"] = [{"role": "user", "content": "hello"}]
        _native.write_span(d)

        results = _native.query_spans_by_trace(str(db_path), d["trace_id"])
        assert len(results) == 1
        span = results[0]
        assert span["model"] == "gpt-4o"
        assert span["provider"] == "openai"
        assert span["input_tokens"] == 500
        assert span["output_tokens"] == 200
        assert span["input_content_id"] is not None  # Content was captured
```

**Step 2: Run tests**

Run: `PYTHONPATH=python .venv/bin/pytest tests/test_native.py::TestQuerySpansByTrace -v`
Expected: All PASS

**Step 3: Commit**

```bash
git add tests/test_native.py
git commit -m "Add integration tests for query_spans_by_trace roundtrip"
```

---

### Task 5: Run full test suite + existing benchmarks

Verify nothing is broken.

**Step 1: Run all Rust tests**

Run: `cargo test --workspace`
Expected: All PASS

**Step 2: Run all Python tests**

Run: `PYTHONPATH=python .venv/bin/pytest tests/ bench/test_benchmark.py -v`
Expected: All PASS (existing tests should still work — the old write_span behavior was a no-op; now it writes, but tests that mock `_write_root_span` or `write_span` are unaffected)

**Step 3: Run benchmark suite**

Run: `PYTHONPATH=python .venv/bin/python -m bench.run --quick`
Expected: All PASS

**Step 4: Commit (if any fixes were needed)**

---

### Task 6: Update existing tests that relied on stub behavior

Some existing tests in `test_native.py` assumed `create_db` and `query_spans_by_trace` were stubs. Update them:

- `TestCreateDb::test_stub_succeeds` — rename to `test_basic_succeeds`, keep as-is (it will now actually create a file in /tmp — that's fine)
- `TestCreateDb::test_canonical_flag` — same, now actually creates schema
- `TestQuerySpansByTrace::test_stub_returns_empty_list` — remove (replaced by roundtrip tests in Task 4)

**Step 1: Update tests, run full suite**

Run: `PYTHONPATH=python .venv/bin/pytest tests/test_native.py -v`
Expected: All PASS

**Step 2: Commit**

```bash
git add tests/test_native.py
git commit -m "Update native FFI tests for real storage behavior"
```

---

### Task 7: Close the bead

```bash
bd close bd-2os.1
```
