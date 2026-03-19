//! SQLite database access layer.
//!
//! Creates per-process and canonical databases with the profiler schema,
//! and provides CRUD operations for spans, content, and pricing.

use std::path::Path;

use anyhow::{bail, Context, Result};
use rusqlite::{params, Connection};

use crate::span::{ContentTable, ModelPricing, Span, TraceSummary};

const SCHEMA_VERSION: i32 = 1;

/// SQL to create the core tables (shared between per-process and canonical DBs).
const CORE_SCHEMA: &str = r#"
CREATE TABLE IF NOT EXISTS spans (
    span_id             TEXT PRIMARY KEY,
    trace_id            TEXT NOT NULL,
    parent_span_id      TEXT,
    name                TEXT NOT NULL,
    kind                TEXT NOT NULL,
    start_time          INTEGER NOT NULL,
    end_time            INTEGER,
    status              TEXT DEFAULT 'OK',
    model               TEXT,
    provider            TEXT,
    input_tokens        INTEGER,
    output_tokens       INTEGER,
    cache_creation_tokens INTEGER,
    cache_read_tokens   INTEGER,
    cost_usd            REAL,
    attributes          TEXT NOT NULL,
    input_content_id    TEXT,
    output_content_id   TEXT,
    input_embedding     BLOB,
    output_embedding    BLOB,
    embedding_model     TEXT DEFAULT 'potion-base-8M'
);
CREATE INDEX IF NOT EXISTS idx_spans_trace_id ON spans(trace_id);
CREATE INDEX IF NOT EXISTS idx_spans_start_time ON spans(start_time);
CREATE INDEX IF NOT EXISTS idx_spans_input_content_id ON spans(input_content_id);
CREATE INDEX IF NOT EXISTS idx_spans_kind ON spans(kind);

CREATE TABLE IF NOT EXISTS input_content (
    content_id      TEXT PRIMARY KEY,
    content_text    BLOB NOT NULL,
    created_at      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS output_content (
    content_id      TEXT PRIMARY KEY,
    content_text    BLOB NOT NULL,
    created_at      INTEGER NOT NULL
);
"#;

/// SQL for canonical-only objects (traces VIEW, model_pricing table).
const CANONICAL_SCHEMA: &str = r#"
CREATE VIEW IF NOT EXISTS traces AS
SELECT trace_id, MIN(start_time) AS start_time, MAX(end_time) AS end_time,
    (SELECT span_id FROM spans s2 WHERE s2.trace_id = s1.trace_id AND s2.parent_span_id IS NULL ORDER BY s2.start_time ASC LIMIT 1) AS root_span_id,
    (SELECT COUNT(*) FROM spans s2 WHERE s2.trace_id = s1.trace_id AND s2.parent_span_id IS NULL) AS root_span_count
FROM spans s1 GROUP BY trace_id;

CREATE TABLE IF NOT EXISTS model_pricing (
    model_id            TEXT PRIMARY KEY,
    input_cost          REAL NOT NULL,
    output_cost         REAL NOT NULL,
    cache_creation_cost REAL,
    cache_read_cost     REAL,
    context_window      INTEGER,
    updated_at          INTEGER NOT NULL,
    source              TEXT DEFAULT 'bundled'
);
"#;

/// Create or open a database at the given path.
///
/// If `is_canonical` is true, also creates the traces VIEW and model_pricing table.
/// Per-process DBs omit these (incomplete data / CLI concern).
///
/// Security: refuses to create databases under symlinked directories.
pub fn create_db(path: &Path, is_canonical: bool) -> Result<Connection> {
    // Security: check for symlink attacks on parent directory.
    if let Some(parent) = path.parent() {
        crate::hardening::check_no_symlink(parent)?;
    }

    // Ensure parent directory exists with 0700 permissions.
    if let Some(parent) = path.parent() {
        if !parent.exists() {
            std::fs::create_dir_all(parent)
                .with_context(|| format!("Failed to create directory {}", parent.display()))?;
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                std::fs::set_permissions(parent, std::fs::Permissions::from_mode(0o700))?;
            }
        }
    }

    let conn = Connection::open(path)
        .with_context(|| format!("Failed to create database at {}", path.display()))?;

    // Set file permissions to 0600.
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600))?;
    }

    // Set PRAGMAs.
    conn.execute_batch(
        "PRAGMA journal_mode = WAL;
         PRAGMA foreign_keys = OFF;
         PRAGMA synchronous = NORMAL;
         PRAGMA busy_timeout = 5000;",
    )?;

    // Check schema version.
    let version: i32 = conn.pragma_query_value(None, "user_version", |row| row.get(0))?;

    if version == 0 {
        // Fresh database — apply schema.
        conn.execute_batch(CORE_SCHEMA)?;
        if is_canonical {
            conn.execute_batch(CANONICAL_SCHEMA)?;
        }
        conn.pragma_update(None, "user_version", SCHEMA_VERSION)?;
    } else if version != SCHEMA_VERSION {
        bail!(
            "Schema version mismatch: expected {}, found {}",
            SCHEMA_VERSION,
            version
        );
    }

    Ok(conn)
}

/// Open an existing database, verifying schema version.
pub fn open_db(path: &Path) -> Result<Connection> {
    let conn = Connection::open(path)
        .with_context(|| format!("Failed to open database at {}", path.display()))?;

    conn.execute_batch(
        "PRAGMA journal_mode = WAL;
         PRAGMA foreign_keys = OFF;
         PRAGMA synchronous = NORMAL;
         PRAGMA busy_timeout = 5000;",
    )?;

    let version: i32 = conn.pragma_query_value(None, "user_version", |row| row.get(0))?;
    if version != SCHEMA_VERSION {
        bail!(
            "Schema version mismatch: expected {}, found {}",
            SCHEMA_VERSION,
            version
        );
    }

    Ok(conn)
}

/// Insert a span (INSERT OR IGNORE — dedup by span_id).
pub fn insert_span(conn: &Connection, span: &Span) -> Result<()> {
    conn.execute(
        "INSERT OR IGNORE INTO spans (
            span_id, trace_id, parent_span_id, name, kind,
            start_time, end_time, status, model, provider,
            input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,
            cost_usd, attributes, input_content_id, output_content_id,
            input_embedding, output_embedding, embedding_model
        ) VALUES (
            ?1, ?2, ?3, ?4, ?5,
            ?6, ?7, ?8, ?9, ?10,
            ?11, ?12, ?13, ?14,
            ?15, ?16, ?17, ?18,
            ?19, ?20, ?21
        )",
        params![
            span.span_id,
            span.trace_id,
            span.parent_span_id,
            span.name,
            span.kind,
            span.start_time,
            span.end_time,
            span.status,
            span.model,
            span.provider,
            span.input_tokens,
            span.output_tokens,
            span.cache_creation_tokens,
            span.cache_read_tokens,
            span.cost_usd,
            span.attributes,
            span.input_content_id,
            span.output_content_id,
            span.input_embedding,
            span.output_embedding,
            span.embedding_model,
        ],
    )?;
    Ok(())
}

/// Insert content blob (INSERT OR IGNORE — dedup by content_id).
pub fn insert_content(
    conn: &Connection,
    table: ContentTable,
    content_id: &str,
    compressed_blob: &[u8],
    created_at: i64,
) -> Result<()> {
    let sql = format!(
        "INSERT OR IGNORE INTO {} (content_id, content_text, created_at) VALUES (?1, ?2, ?3)",
        table.table_name()
    );
    conn.execute(&sql, params![content_id, compressed_blob, created_at])?;
    Ok(())
}

/// Query all spans belonging to a trace.
pub fn query_spans_by_trace(conn: &Connection, trace_id: &str) -> Result<Vec<Span>> {
    let mut stmt = conn.prepare(
        "SELECT span_id, trace_id, parent_span_id, name, kind,
                start_time, end_time, status, model, provider,
                input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,
                cost_usd, attributes, input_content_id, output_content_id,
                input_embedding, output_embedding, embedding_model
         FROM spans WHERE trace_id = ?1 ORDER BY start_time",
    )?;

    let spans = stmt
        .query_map(params![trace_id], |row| {
            Ok(Span {
                span_id: row.get(0)?,
                trace_id: row.get(1)?,
                parent_span_id: row.get(2)?,
                name: row.get(3)?,
                kind: row.get(4)?,
                start_time: row.get(5)?,
                end_time: row.get(6)?,
                status: row.get(7)?,
                model: row.get(8)?,
                provider: row.get(9)?,
                input_tokens: row.get(10)?,
                output_tokens: row.get(11)?,
                cache_creation_tokens: row.get(12)?,
                cache_read_tokens: row.get(13)?,
                cost_usd: row.get(14)?,
                attributes: row.get(15)?,
                input_content_id: row.get(16)?,
                output_content_id: row.get(17)?,
                input_embedding: row.get(18)?,
                output_embedding: row.get(19)?,
                embedding_model: row.get(20)?,
            })
        })?
        .collect::<std::result::Result<Vec<_>, _>>()?;

    Ok(spans)
}

/// Query trace summaries from the traces VIEW (canonical DB only).
pub fn query_traces(
    conn: &Connection,
    limit: i64,
    since: Option<i64>,
) -> Result<Vec<TraceSummary>> {
    let (sql, params_vec): (String, Vec<Box<dyn rusqlite::types::ToSql>>) = match since {
        Some(ts) => (
            "SELECT trace_id, start_time, end_time, root_span_id, root_span_count \
             FROM traces WHERE start_time >= ?1 ORDER BY start_time DESC LIMIT ?2"
                .to_string(),
            vec![Box::new(ts), Box::new(limit)],
        ),
        None => (
            "SELECT trace_id, start_time, end_time, root_span_id, root_span_count \
             FROM traces ORDER BY start_time DESC LIMIT ?1"
                .to_string(),
            vec![Box::new(limit)],
        ),
    };

    let mut stmt = conn.prepare(&sql)?;
    let params_refs: Vec<&dyn rusqlite::types::ToSql> = params_vec.iter().map(|p| p.as_ref()).collect();
    let traces = stmt
        .query_map(params_refs.as_slice(), |row| {
            Ok(TraceSummary {
                trace_id: row.get(0)?,
                start_time: row.get(1)?,
                end_time: row.get(2)?,
                root_span_id: row.get(3)?,
                root_span_count: row.get(4)?,
            })
        })?
        .collect::<std::result::Result<Vec<_>, _>>()?;

    Ok(traces)
}

/// Stats from a migration operation.
#[derive(Debug, Clone)]
pub struct MigrationStats {
    pub old_version: i32,
    pub new_version: i32,
    pub migrations_applied: usize,
}

/// Apply forward-compatible schema migrations to a database.
///
/// Checks PRAGMA user_version and applies any needed ALTER TABLE statements.
/// Current schema version is 1 — future versions will add migrations here.
///
/// Returns `Ok(None)` if no migration was needed.
/// Returns `Ok(Some(stats))` if migrations were applied.
/// Returns `Err` if the version is incompatible (newer than expected).
pub fn migrate_db(conn: &Connection) -> Result<Option<MigrationStats>> {
    let version: i32 = conn.pragma_query_value(None, "user_version", |row| row.get(0))?;

    if version == SCHEMA_VERSION {
        return Ok(None);
    }

    if version > SCHEMA_VERSION {
        bail!(
            "Incompatible schema version {}. Expected <= {}. \
             Run 'agentc migrate' to update your trace database.",
            version,
            SCHEMA_VERSION
        );
    }

    let old_version = version;
    let migrations_applied = 0;

    // Apply migrations sequentially from old_version to SCHEMA_VERSION.
    // Currently only version 1 exists, so no migrations needed yet.
    // Future migrations would go here:
    // if version < 2 { conn.execute_batch("ALTER TABLE ..."); version = 2; migrations_applied += 1; }

    conn.pragma_update(None, "user_version", SCHEMA_VERSION)?;

    Ok(Some(MigrationStats {
        old_version,
        new_version: SCHEMA_VERSION,
        migrations_applied,
    }))
}

/// Insert model pricing. Bundled pricing uses INSERT OR IGNORE (never overwrites).
/// User pricing uses INSERT OR REPLACE (overwrites bundled).
pub fn insert_pricing(conn: &Connection, pricing: &ModelPricing) -> Result<()> {
    let sql = if pricing.source == "user" {
        "INSERT OR REPLACE INTO model_pricing (
            model_id, input_cost, output_cost, cache_creation_cost, cache_read_cost,
            context_window, updated_at, source
        ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)"
    } else {
        "INSERT OR IGNORE INTO model_pricing (
            model_id, input_cost, output_cost, cache_creation_cost, cache_read_cost,
            context_window, updated_at, source
        ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)"
    };

    conn.execute(
        sql,
        params![
            pricing.model_id,
            pricing.input_cost,
            pricing.output_cost,
            pricing.cache_creation_cost,
            pricing.cache_read_cost,
            pricing.context_window,
            pricing.updated_at,
            pricing.source,
        ],
    )?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    fn test_span(id: &str, trace_id: &str) -> Span {
        Span {
            span_id: id.to_string(),
            trace_id: trace_id.to_string(),
            parent_span_id: None,
            name: "test_call".to_string(),
            kind: "chat".to_string(),
            start_time: 1000000,
            end_time: Some(2000000),
            status: "OK".to_string(),
            model: Some("claude-sonnet-4-20250514".to_string()),
            provider: Some("anthropic".to_string()),
            input_tokens: Some(100),
            output_tokens: Some(50),
            cache_creation_tokens: None,
            cache_read_tokens: None,
            cost_usd: None,
            attributes: "{}".to_string(),
            input_content_id: None,
            output_content_id: None,
            input_embedding: None,
            output_embedding: None,
            embedding_model: Some("potion-base-8M".to_string()),
        }
    }

    #[test]
    fn test_create_db_canonical_schema() {
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("test.db");
        let conn = create_db(&path, true).unwrap();

        // Check user_version.
        let version: i32 = conn
            .pragma_query_value(None, "user_version", |row| row.get(0))
            .unwrap();
        assert_eq!(version, SCHEMA_VERSION);

        // Check journal_mode is WAL.
        let journal: String = conn
            .pragma_query_value(None, "journal_mode", |row| row.get(0))
            .unwrap();
        assert_eq!(journal, "wal");

        // Check foreign_keys is OFF.
        let fk: i32 = conn
            .pragma_query_value(None, "foreign_keys", |row| row.get(0))
            .unwrap();
        assert_eq!(fk, 0);

        // Check synchronous is NORMAL (1).
        let sync: i32 = conn
            .pragma_query_value(None, "synchronous", |row| row.get(0))
            .unwrap();
        assert_eq!(sync, 1);

        // Check indexes exist.
        let mut stmt = conn.prepare("SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_spans_%'").unwrap();
        let indexes: Vec<String> = stmt
            .query_map([], |row| row.get(0))
            .unwrap()
            .collect::<std::result::Result<Vec<_>, _>>()
            .unwrap();
        assert!(indexes.contains(&"idx_spans_trace_id".to_string()));
        assert!(indexes.contains(&"idx_spans_start_time".to_string()));
        assert!(indexes.contains(&"idx_spans_input_content_id".to_string()));
        assert!(indexes.contains(&"idx_spans_kind".to_string()));

        // Check traces VIEW exists.
        let view_count: i32 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='view' AND name='traces'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(view_count, 1);

        // Check model_pricing table exists.
        let table_count: i32 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='model_pricing'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(table_count, 1);
    }

    #[test]
    fn test_create_db_per_process_omits_canonical() {
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("pid-12345.db");
        let conn = create_db(&path, false).unwrap();

        // traces VIEW should NOT exist.
        let view_count: i32 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='view' AND name='traces'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(view_count, 0);

        // model_pricing should NOT exist.
        let table_count: i32 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='model_pricing'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(table_count, 0);
    }

    #[test]
    fn test_insert_span_and_query_roundtrip() {
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("test.db");
        let conn = create_db(&path, false).unwrap();

        let s1 = test_span("span-1", "trace-1");
        let mut s2 = test_span("span-2", "trace-1");
        s2.parent_span_id = Some("span-1".to_string());
        s2.start_time = 1500000;
        let s3 = test_span("span-3", "trace-1");

        insert_span(&conn, &s1).unwrap();
        insert_span(&conn, &s2).unwrap();
        insert_span(&conn, &s3).unwrap();

        let result = query_spans_by_trace(&conn, "trace-1").unwrap();
        assert_eq!(result.len(), 3);
        assert_eq!(result[0].span_id, "span-1");
        assert_eq!(result[2].span_id, "span-2"); // span-2 has later start_time
    }

    #[test]
    fn test_insert_span_duplicate_ignored() {
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("test.db");
        let conn = create_db(&path, false).unwrap();

        let span = test_span("span-dup", "trace-1");
        insert_span(&conn, &span).unwrap();
        insert_span(&conn, &span).unwrap(); // Should not error.

        let result = query_spans_by_trace(&conn, "trace-1").unwrap();
        assert_eq!(result.len(), 1);
    }

    #[test]
    fn test_insert_content_dedup() {
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("test.db");
        let conn = create_db(&path, false).unwrap();

        let blob = b"compressed data";
        insert_content(&conn, ContentTable::InputContent, "abc123", blob, 1000000).unwrap();
        insert_content(&conn, ContentTable::InputContent, "abc123", blob, 2000000).unwrap();

        let count: i32 = conn
            .query_row(
                "SELECT COUNT(*) FROM input_content WHERE content_id = 'abc123'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(count, 1);
    }

    #[test]
    fn test_insert_content_tables() {
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("test.db");
        let conn = create_db(&path, false).unwrap();

        insert_content(&conn, ContentTable::InputContent, "in-1", b"input", 1000).unwrap();
        insert_content(&conn, ContentTable::OutputContent, "out-1", b"output", 1000).unwrap();

        let in_count: i32 = conn
            .query_row("SELECT COUNT(*) FROM input_content", [], |row| row.get(0))
            .unwrap();
        let out_count: i32 = conn
            .query_row("SELECT COUNT(*) FROM output_content", [], |row| row.get(0))
            .unwrap();
        assert_eq!(in_count, 1);
        assert_eq!(out_count, 1);
    }

    #[test]
    fn test_query_traces_view() {
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("test.db");
        let conn = create_db(&path, true).unwrap();

        // Insert spans for 2 traces.
        let s1 = test_span("s1", "t1");
        let mut s2 = test_span("s2", "t1");
        s2.parent_span_id = Some("s1".to_string());
        s2.start_time = 1500000;
        let mut s3 = test_span("s3", "t2");
        s3.start_time = 3000000;
        s3.end_time = Some(4000000);

        insert_span(&conn, &s1).unwrap();
        insert_span(&conn, &s2).unwrap();
        insert_span(&conn, &s3).unwrap();

        let traces = query_traces(&conn, 10, None).unwrap();
        assert_eq!(traces.len(), 2);

        // Most recent first.
        assert_eq!(traces[0].trace_id, "t2");
        assert_eq!(traces[0].root_span_count, 1);
        assert_eq!(traces[1].trace_id, "t1");
        assert_eq!(traces[1].root_span_count, 1); // s1 is root, s2 has parent
    }

    #[test]
    fn test_query_traces_with_since() {
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("test.db");
        let conn = create_db(&path, true).unwrap();

        let s1 = test_span("s1", "t1");
        let mut s2 = test_span("s2", "t2");
        s2.start_time = 5000000;
        s2.end_time = Some(6000000);

        insert_span(&conn, &s1).unwrap();
        insert_span(&conn, &s2).unwrap();

        // Only traces since 3000000.
        let traces = query_traces(&conn, 10, Some(3000000)).unwrap();
        assert_eq!(traces.len(), 1);
        assert_eq!(traces[0].trace_id, "t2");
    }

    #[test]
    fn test_query_traces_respects_limit() {
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("test.db");
        let conn = create_db(&path, true).unwrap();

        for i in 0..5 {
            let mut span = test_span(&format!("s{i}"), &format!("t{i}"));
            span.start_time = (i as i64 + 1) * 1000000;
            insert_span(&conn, &span).unwrap();
        }

        let traces = query_traces(&conn, 2, None).unwrap();
        assert_eq!(traces.len(), 2);
    }

    #[test]
    fn test_multi_root_trace() {
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("test.db");
        let conn = create_db(&path, true).unwrap();

        // Two root spans in the same trace.
        let mut s1 = test_span("root1", "multi-root");
        s1.start_time = 1000;
        let mut s2 = test_span("root2", "multi-root");
        s2.start_time = 2000;
        insert_span(&conn, &s1).unwrap();
        insert_span(&conn, &s2).unwrap();

        let traces = query_traces(&conn, 10, None).unwrap();
        assert_eq!(traces.len(), 1);
        assert_eq!(traces[0].root_span_count, 2);
        assert_eq!(traces[0].root_span_id.as_deref(), Some("root1"));
    }

    #[test]
    fn test_insert_pricing_bundled_no_overwrite() {
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("test.db");
        let conn = create_db(&path, true).unwrap();

        let pricing = ModelPricing {
            model_id: "claude-sonnet-4-20250514".to_string(),
            input_cost: 3.0,
            output_cost: 15.0,
            cache_creation_cost: Some(3.75),
            cache_read_cost: Some(0.3),
            context_window: Some(200000),
            updated_at: 1000000,
            source: "bundled".to_string(),
        };
        insert_pricing(&conn, &pricing).unwrap();

        // Try to overwrite with different bundled price — should be ignored.
        let pricing2 = ModelPricing {
            input_cost: 999.0,
            ..pricing.clone()
        };
        insert_pricing(&conn, &pricing2).unwrap();

        let cost: f64 = conn
            .query_row(
                "SELECT input_cost FROM model_pricing WHERE model_id = ?1",
                params!["claude-sonnet-4-20250514"],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(cost, 3.0); // Original preserved.
    }

    #[test]
    fn test_insert_pricing_user_overwrites() {
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("test.db");
        let conn = create_db(&path, true).unwrap();

        let bundled = ModelPricing {
            model_id: "gpt-4o".to_string(),
            input_cost: 5.0,
            output_cost: 15.0,
            cache_creation_cost: None,
            cache_read_cost: None,
            context_window: Some(128000),
            updated_at: 1000000,
            source: "bundled".to_string(),
        };
        insert_pricing(&conn, &bundled).unwrap();

        let user = ModelPricing {
            input_cost: 2.5,
            output_cost: 10.0,
            updated_at: 2000000,
            source: "user".to_string(),
            ..bundled
        };
        insert_pricing(&conn, &user).unwrap();

        let cost: f64 = conn
            .query_row(
                "SELECT input_cost FROM model_pricing WHERE model_id = ?1",
                params!["gpt-4o"],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(cost, 2.5); // User override applied.
    }

    #[test]
    fn test_schema_version_mismatch() {
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("test.db");

        // Create DB, then change user_version.
        {
            let conn = create_db(&path, false).unwrap();
            conn.pragma_update(None, "user_version", 999).unwrap();
        }

        // Re-open should fail.
        let result = open_db(&path);
        assert!(result.is_err());
        let err = result.unwrap_err().to_string();
        assert!(err.contains("Schema version mismatch"));
    }

    #[cfg(unix)]
    #[test]
    fn test_file_permissions() {
        use std::os::unix::fs::PermissionsExt;

        let dir = TempDir::new().unwrap();
        let subdir = dir.path().join("agentc");
        let path = subdir.join("test.db");
        let _conn = create_db(&path, false).unwrap();

        let dir_mode = std::fs::metadata(&subdir).unwrap().permissions().mode() & 0o777;
        assert_eq!(dir_mode, 0o700);

        let file_mode = std::fs::metadata(&path).unwrap().permissions().mode() & 0o777;
        assert_eq!(file_mode, 0o600);
    }

    #[test]
    fn test_integration_100_spans_5_traces() {
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("test.db");
        let conn = create_db(&path, true).unwrap();

        for trace_idx in 0..5 {
            let trace_id = format!("trace-{trace_idx}");
            for span_idx in 0..20 {
                let mut span = test_span(
                    &format!("span-{trace_idx}-{span_idx}"),
                    &trace_id,
                );
                span.start_time = (trace_idx * 1000000 + span_idx * 1000) as i64;
                span.end_time = Some(span.start_time + 500);
                if span_idx > 0 {
                    span.parent_span_id =
                        Some(format!("span-{trace_idx}-0"));
                }
                insert_span(&conn, &span).unwrap();
            }
        }

        let traces = query_traces(&conn, 100, None).unwrap();
        assert_eq!(traces.len(), 5);

        // Each trace has 1 root span (span_idx=0) and 19 children.
        for t in &traces {
            assert_eq!(t.root_span_count, 1);
        }

        // Verify span counts per trace.
        for trace_idx in 0..5 {
            let spans = query_spans_by_trace(&conn, &format!("trace-{trace_idx}")).unwrap();
            assert_eq!(spans.len(), 20);
        }
    }
}
