//! Agentc CLI binary.
//!
//! Subcommands: record, traces, export.

mod helpers;

use std::env;
use std::fs;
use std::io::Write;
use std::os::unix::fs::PermissionsExt;
use std::process::{Command, ExitCode};

use clap::Parser;

use agentc_analyzer::report::{
    render_traces_list, TracesList, TracesListRow,
};
use agentc_analyzer::{cost, waste};
use helpers::{
    open_canonical_db_if_exists, parse_since_date, resolve_storage_path,
    try_merge_pending,
};

#[derive(Parser)]
#[command(name = "agentc", about = "JIT optimization runtime for LLM agent workloads")]
enum Cli {
    /// Record an agent session (wraps a Python command).
    Record {
        /// Command to execute.
        #[arg(trailing_var_arg = true, required = true)]
        command: Vec<String>,

        /// Storage path override.
        #[arg(long, default_value = "~/.agentc")]
        storage_path: String,

        /// Capture full prompt/response text.
        #[arg(long, default_value_t = true)]
        capture_content: bool,

        /// Compute embeddings.
        #[arg(long)]
        capture_embeddings: Option<bool>,

        /// Fail open on profiler errors.
        #[arg(long, default_value_t = true)]
        fail_open: bool,
    },
    /// Show profiler traces and analysis.
    Traces {
        /// Filter by trace ID prefix.
        #[arg(long)]
        trace_id: Option<String>,
        /// Maximum number of traces to display.
        #[arg(long, default_value_t = 20)]
        limit: usize,
        /// Only show traces after this date (YYYY-MM-DD).
        #[arg(long)]
        since: Option<String>,
        /// Output format (table, json).
        #[arg(long, default_value = "table")]
        format: String,
        /// Storage path.
        #[arg(long, default_value = "~/.agentc")]
        storage_path: String,
    },
    /// Analyze a single trace (cost breakdown + waste detection).
    Analyze {
        /// Trace ID (prefix match). Omit for most recent trace.
        trace_id: Option<String>,
        /// Storage path.
        #[arg(long, default_value = "~/.agentc")]
        storage_path: String,
    },
    /// Aggregate report across multiple traces.
    Report {
        /// Number of most recent traces to analyze.
        #[arg(long)]
        last: Option<usize>,
        /// Only include traces after this date (YYYY-MM-DD).
        #[arg(long)]
        since: Option<String>,
        /// Filter by agent name.
        #[arg(long)]
        agent: Option<String>,
        /// Filter by model ID.
        #[arg(long)]
        model: Option<String>,
        /// Storage path.
        #[arg(long, default_value = "~/.agentc")]
        storage_path: String,
    },
    /// Compute embeddings for spans missing them.
    Embed {
        /// Backfill NULL embeddings from stored content.
        #[arg(long)]
        backfill: bool,
        /// Storage path.
        #[arg(long, default_value = "~/.agentc")]
        storage_path: String,
    },
    /// Apply schema migrations to traces.db.
    Migrate {
        /// Storage path.
        #[arg(long, default_value = "~/.agentc")]
        storage_path: String,
    },
    /// Export traces in OTLP format.
    Export {
        /// Trace ID (prefix match, min 4 chars).
        trace_id: String,
        /// Output file path (stdout if omitted).
        #[arg(long)]
        output: Option<String>,
        /// Export format (otlp-json).
        #[arg(long, default_value = "otlp-json")]
        format: String,
        /// Storage path.
        #[arg(long, default_value = "~/.agentc")]
        storage_path: String,
    },
}

/// The sitecustomize.py content that auto-initializes agentc.
///
/// Chains with any existing sitecustomize.py in the user's environment.
const SITECUSTOMIZE_PY: &str = r#"
import sys as _sys
import os as _os

# Remove our temp dir from sys.path to find the real sitecustomize (if any)
_agentc_tmp = _os.path.dirname(_os.path.abspath(__file__))
_orig_path = _sys.path[:]
try:
    _sys.path = [p for p in _sys.path if _os.path.abspath(p) != _agentc_tmp]
    try:
        import importlib
        _spec = importlib.util.find_spec("sitecustomize")
        if _spec is not None:
            _mod = importlib.util.module_from_spec(_spec)
            _spec.loader.exec_module(_mod)
    except Exception:
        pass  # No original sitecustomize or it failed
finally:
    _sys.path = _orig_path

# Initialize agentc
try:
    import agentc
    agentc.init()
except Exception as _e:
    import logging
    logging.getLogger("agentc").debug("agentc.init() failed in sitecustomize: %s", _e)
"#;

fn cmd_record(
    command: Vec<String>,
    storage_path: String,
    capture_content: bool,
    capture_embeddings: Option<bool>,
    fail_open: bool,
) -> anyhow::Result<ExitCode> {
    if command.is_empty() {
        anyhow::bail!("agentc record: no command specified");
    }

    // Create temp directory for sitecustomize.py
    let tmp_dir = tempfile::TempDir::new()?;
    let tmp_path = tmp_dir.path();

    // Set permissions to 0700
    fs::set_permissions(tmp_path, fs::Permissions::from_mode(0o700))?;

    // Write sitecustomize.py
    let site_path = tmp_path.join("sitecustomize.py");
    let mut file = fs::File::create(&site_path)?;
    file.write_all(SITECUSTOMIZE_PY.as_bytes())?;
    fs::set_permissions(&site_path, fs::Permissions::from_mode(0o600))?;

    // Build modified PYTHONPATH (prepend temp dir)
    let existing_pythonpath = env::var("PYTHONPATH").unwrap_or_default();
    let new_pythonpath = if existing_pythonpath.is_empty() {
        tmp_path.to_string_lossy().to_string()
    } else {
        format!("{}:{}", tmp_path.display(), existing_pythonpath)
    };

    // Build environment variables
    let mut child_env: Vec<(String, String)> = vec![
        ("PYTHONPATH".to_string(), new_pythonpath),
        (
            "AGENTC_STORAGE_PATH".to_string(),
            storage_path.clone(),
        ),
        (
            "AGENTC_CAPTURE_CONTENT".to_string(),
            capture_content.to_string(),
        ),
        (
            "AGENTC_FAIL_OPEN".to_string(),
            fail_open.to_string(),
        ),
    ];
    if let Some(embed) = capture_embeddings {
        child_env.push((
            "AGENTC_CAPTURE_EMBEDDINGS".to_string(),
            embed.to_string(),
        ));
    }

    // Spawn child process
    let status = Command::new(&command[0])
        .args(&command[1..])
        .envs(child_env.iter().map(|(k, v)| (k.as_str(), v.as_str())))
        .status()?;

    // Temp dir cleaned up automatically when tmp_dir drops

    // Exit code propagation
    let exit_code = if let Some(code) = status.code() {
        code as u8
    } else {
        // Killed by signal
        #[cfg(unix)]
        {
            use std::os::unix::process::ExitStatusExt;
            if let Some(sig) = status.signal() {
                (128 + sig) as u8
            } else {
                1
            }
        }
        #[cfg(not(unix))]
        {
            1
        }
    };

    // TODO(VelvetHammer, bd-2ix/bd-36j): Post-exit summary
    // - Merge-on-read
    // - Cost backfill
    // - Waste detection
    // - Print summary line

    Ok(ExitCode::from(exit_code))
}

fn main() -> anyhow::Result<ExitCode> {
    let cli = Cli::parse();

    match cli {
        Cli::Record {
            command,
            storage_path,
            capture_content,
            capture_embeddings,
            fail_open,
        } => cmd_record(command, storage_path, capture_content, capture_embeddings, fail_open),
        Cli::Traces {
            trace_id,
            limit,
            since,
            format: _format,
            storage_path,
        } => {
            cmd_traces(trace_id, limit, since, storage_path)?;
            Ok(ExitCode::SUCCESS)
        }
        Cli::Analyze {
            trace_id,
            storage_path,
        } => {
            cmd_analyze(trace_id, storage_path)?;
            Ok(ExitCode::SUCCESS)
        }
        Cli::Report {
            last,
            since,
            agent,
            model,
            storage_path,
        } => {
            cmd_report(last, since, agent, model, storage_path)?;
            Ok(ExitCode::SUCCESS)
        }
        Cli::Embed {
            backfill,
            storage_path,
        } => {
            cmd_embed(backfill, storage_path)?;
            Ok(ExitCode::SUCCESS)
        }
        Cli::Migrate { storage_path } => {
            cmd_migrate(storage_path)?;
            Ok(ExitCode::SUCCESS)
        }
        Cli::Export {
            trace_id,
            output,
            format: _format,
            storage_path,
        } => {
            cmd_export(trace_id, output, storage_path)?;
            Ok(ExitCode::SUCCESS)
        }
    }
}

fn cmd_analyze(
    trace_id: Option<String>,
    _storage_path: String,
) -> anyhow::Result<()> {
    // Validate trace ID prefix if provided
    if let Some(ref tid) = trace_id {
        if tid.len() < 4 {
            anyhow::bail!(
                "Trace ID prefix must be at least 4 characters, got: '{}'",
                tid
            );
        }
    }

    let display_id = trace_id
        .as_deref()
        .unwrap_or("(most recent)");

    // TODO(VelvetHammer, bd-2db): Query trace from storage
    // TODO(VelvetHammer, bd-2ix): Cost backfill
    // TODO(VelvetHammer, bd-2va): Waste detection

    println!("CALL BREAKDOWN — trace {}", display_id);
    println!(
        "{:>4} {:<16} {:<24} {:>8} {:>8} {:>8} FLAGS",
        "#", "AGENT", "MODEL", "IN", "OUT", "COST"
    );
    println!("{}", "-".repeat(80));
    println!("(no spans found — storage backend not yet connected)");
    println!();
    println!("WASTE REPORT");
    println!("(no waste flags detected)");

    Ok(())
}

fn cmd_report(
    last: Option<usize>,
    since: Option<String>,
    agent: Option<String>,
    model: Option<String>,
    _storage_path: String,
) -> anyhow::Result<()> {
    let _ = (last, since, agent, model);

    // TODO(VelvetHammer, bd-2db): Query traces from storage

    println!("SUMMARY");
    println!("  Traces: 0");
    println!("  Spans:  0");
    println!("  Tokens: 0");
    println!("  Cost:   $0.00");
    println!();
    println!("BY MODEL");
    println!(
        "  {:<24} {:>8} {:>8} {:>8} {:>8}",
        "MODEL", "CALLS", "IN", "OUT", "COST"
    );
    println!("  (no data)");
    println!();
    println!("BY AGENT");
    println!(
        "  {:<24} {:>8} {:>8} {:>8} {:>8}",
        "AGENT", "CALLS", "IN", "OUT", "COST"
    );
    println!("  (no data)");
    println!();
    println!("WASTE SUMMARY");
    println!("  (no waste flags detected)");

    Ok(())
}

fn cmd_traces(
    trace_id: Option<String>,
    limit: usize,
    since: Option<String>,
    storage_path: String,
) -> anyhow::Result<()> {
    let storage_dir = resolve_storage_path(&storage_path);
    try_merge_pending();

    let Some(conn) = open_canonical_db_if_exists(&storage_dir)? else {
        print!("{}", render_traces_list(&TracesList::default()));
        return Ok(());
    };

    // Backfill costs so cost_usd columns are populated.
    cost::full_cost_backfill(&conn)?;

    let since_us = match since {
        Some(ref s) => Some(parse_since_date(s)?),
        None => None,
    };
    let summaries = agentc_core::db::query_traces(&conn, limit as i64, since_us)?;

    let filtered: Vec<_> = match trace_id.as_deref() {
        Some(prefix) => summaries
            .into_iter()
            .filter(|t| t.trace_id.starts_with(prefix))
            .collect(),
        None => summaries,
    };

    let mut rows = Vec::with_capacity(filtered.len());
    for summary in &filtered {
        let row = build_traces_list_row(&conn, summary)?;
        rows.push(row);
    }

    print!("{}", render_traces_list(&TracesList { rows }));
    Ok(())
}

/// Aggregate span stats for one trace and run waste analysis to build a `TracesListRow`.
fn build_traces_list_row(
    conn: &rusqlite::Connection,
    summary: &agentc_core::span::TraceSummary,
) -> anyhow::Result<TracesListRow> {
    // Aggregate span stats.
    let (span_count, total_tokens, total_cost): (i64, i64, f64) = conn.query_row(
        "SELECT COUNT(*),
                COALESCE(SUM(COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0)), 0),
                COALESCE(SUM(cost_usd), 0.0)
         FROM spans WHERE trace_id = ?1",
        rusqlite::params![summary.trace_id],
        |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
    )?;

    let duration_us = summary
        .end_time
        .map(|end| (end - summary.start_time).max(0))
        .unwrap_or(0);

    // Waste flag counts.
    let analysis = waste::analyze_trace(conn, &summary.trace_id)?;
    let mut flag_counts: std::collections::BTreeMap<String, usize> =
        std::collections::BTreeMap::new();
    for flag in &analysis.flags {
        *flag_counts.entry(flag.detector.clone()).or_insert(0) += 1;
    }

    Ok(TracesListRow {
        trace_id: summary.trace_id.clone(),
        started_us: summary.start_time,
        duration_us,
        span_count: span_count as usize,
        total_tokens,
        total_cost_usd: total_cost,
        flag_counts: flag_counts.into_iter().collect(),
    })
}

fn cmd_export(
    trace_id: String,
    output: Option<String>,
    _storage_path: String,
) -> anyhow::Result<()> {
    // Validate trace ID prefix (min 4 chars)
    if trace_id.len() < 4 {
        anyhow::bail!(
            "Trace ID prefix must be at least 4 characters, got: '{}'",
            trace_id
        );
    }

    // TODO(VelvetHammer, bd-2db): Query spans by trace ID prefix

    // Build OTLP JSON structure (empty for now)
    let otlp_json = serde_json::json!({
        "resourceSpans": [{
            "resource": {
                "attributes": [{
                    "key": "service.name",
                    "value": { "stringValue": "agentc" }
                }]
            },
            "scopeSpans": [{
                "scope": { "name": "agentc", "version": env!("CARGO_PKG_VERSION") },
                "spans": []
            }]
        }]
    });

    let json_str = serde_json::to_string_pretty(&otlp_json)?;

    match output {
        Some(path) => {
            fs::write(&path, &json_str)?;
            eprintln!("Exported to {}", path);
        }
        None => {
            println!("{}", json_str);
        }
    }

    Ok(())
}

fn cmd_embed(backfill: bool, storage_path: String) -> anyhow::Result<()> {
    if !backfill {
        println!("Use --backfill to compute embeddings for spans missing them.");
        return Ok(());
    }

    let storage_dir = resolve_storage_path(&storage_path);
    agentc_core::hardening::audit_storage_dir(&storage_dir)?;
    let db_path = storage_dir.join("traces.db");

    if !db_path.exists() {
        println!("No traces.db found at {}. Nothing to backfill.", db_path.display());
        return Ok(());
    }

    // Merge any pending per-process DBs first.
    #[cfg(unix)]
    {
        let merge_stats = agentc_core::merge::merge_all_pending()?;
        if merge_stats.spans_merged > 0 {
            println!("Merged {} pending spans before backfill.", merge_stats.spans_merged);
        }
    }

    let conn = agentc_core::db::open_db(&db_path)?;

    println!("Backfilling embeddings for spans with NULL input/output embeddings...");
    let stats = agentc_core::embedding::backfill_embeddings(&conn)?;

    println!("  Examined:    {} spans", stats.total);
    println!("  Computed:    {} embeddings", stats.computed);
    println!("  Failed:      {}", stats.failed);
    println!("  Skipped:     {} (no stored content)", stats.skipped_null_content);

    Ok(())
}

fn cmd_migrate(storage_path: String) -> anyhow::Result<()> {
    let storage_dir = resolve_storage_path(&storage_path);
    agentc_core::hardening::audit_storage_dir(&storage_dir)?;
    let db_path = storage_dir.join("traces.db");

    if !db_path.exists() {
        // Create a fresh canonical DB with current schema.
        println!("No traces.db found. Creating fresh database at {}...", db_path.display());
        let _conn = agentc_core::db::create_db(&db_path, true)?;
        println!("Created traces.db (schema version 1).");
        return Ok(());
    }

    let conn = rusqlite::Connection::open(&db_path)?;
    conn.execute_batch(
        "PRAGMA journal_mode = WAL;
         PRAGMA foreign_keys = OFF;
         PRAGMA synchronous = NORMAL;",
    )?;

    println!("Checking schema version...");
    match agentc_core::db::migrate_db(&conn)? {
        Some(stats) => {
            println!(
                "Migrated from version {} to {} ({} migrations applied).",
                stats.old_version, stats.new_version, stats.migrations_applied
            );
        }
        None => {
            println!("Schema is up to date. No migrations needed.");
        }
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_sitecustomize_content() {
        assert!(SITECUSTOMIZE_PY.contains("import agentc"));
        assert!(SITECUSTOMIZE_PY.contains("agentc.init()"));
    }

    #[test]
    fn test_sitecustomize_chains_original() {
        assert!(SITECUSTOMIZE_PY.contains("find_spec"));
        assert!(SITECUSTOMIZE_PY.contains("sitecustomize"));
    }

    #[test]
    fn test_export_trace_id_min_length() {
        let result = cmd_export("abc".to_string(), None, "~/.agentc".to_string());
        assert!(result.is_err());
        let err = result.unwrap_err().to_string();
        assert!(err.contains("at least 4 characters"));
    }

    #[test]
    fn test_export_valid_prefix() {
        let result = cmd_export("abcd1234".to_string(), None, "~/.agentc".to_string());
        assert!(result.is_ok());
    }

    #[test]
    fn test_traces_empty_storage() {
        // Nonexistent storage dir renders an empty list without error.
        let dir = tempfile::TempDir::new().unwrap();
        let result = cmd_traces(None, 20, None, dir.path().to_string_lossy().into_owned());
        assert!(result.is_ok());
    }

    #[test]
    fn test_traces_with_data() {
        let dir = tempfile::TempDir::new().unwrap();
        let db_path = dir.path().join("traces.db");
        let conn = agentc_core::db::create_db(&db_path, true).unwrap();

        let span = agentc_core::span::Span {
            span_id: "span-1".to_string(),
            trace_id: "abcd1234efgh5678".to_string(),
            parent_span_id: None,
            name: "test".to_string(),
            kind: "chat".to_string(),
            start_time: 1_000_000_000_000_000,
            end_time: Some(1_000_000_500_000_000),
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
            embedding_model: None,
        };
        agentc_core::db::insert_span(&conn, &span).unwrap();
        drop(conn);

        let result = cmd_traces(None, 20, None, dir.path().to_string_lossy().into_owned());
        assert!(result.is_ok(), "got error: {:?}", result.err());
    }

    #[test]
    fn test_traces_prefix_filter_no_match() {
        let dir = tempfile::TempDir::new().unwrap();
        let db_path = dir.path().join("traces.db");
        let conn = agentc_core::db::create_db(&db_path, true).unwrap();
        drop(conn);

        // Empty DB + prefix filter should still succeed (renders empty list).
        let result = cmd_traces(
            Some("zzzz".to_string()),
            20,
            None,
            dir.path().to_string_lossy().into_owned(),
        );
        assert!(result.is_ok());
    }

    #[test]
    fn test_traces_invalid_since_date() {
        let dir = tempfile::TempDir::new().unwrap();
        let db_path = dir.path().join("traces.db");
        let _ = agentc_core::db::create_db(&db_path, true).unwrap();

        let result = cmd_traces(
            None,
            20,
            Some("not-a-date".to_string()),
            dir.path().to_string_lossy().into_owned(),
        );
        assert!(result.is_err());
    }

    #[test]
    fn test_analyze_no_trace_id() {
        let result = cmd_analyze(None, "~/.agentc".to_string());
        assert!(result.is_ok());
    }

    #[test]
    fn test_analyze_with_valid_prefix() {
        let result = cmd_analyze(Some("abcd1234".to_string()), "~/.agentc".to_string());
        assert!(result.is_ok());
    }

    #[test]
    fn test_analyze_short_prefix_fails() {
        let result = cmd_analyze(Some("ab".to_string()), "~/.agentc".to_string());
        assert!(result.is_err());
    }

    #[test]
    fn test_report_runs_without_error() {
        let result = cmd_report(Some(10), None, None, None, "~/.agentc".to_string());
        assert!(result.is_ok());
    }

    #[test]
    fn test_embed_no_backfill_flag() {
        // Without --backfill, should just print usage hint.
        let result = cmd_embed(false, "/tmp/nonexistent-agentc".to_string());
        assert!(result.is_ok());
    }

    #[test]
    fn test_embed_backfill_no_db() {
        // With --backfill but no traces.db, should succeed with message.
        let dir = tempfile::TempDir::new().unwrap();
        let result = cmd_embed(true, dir.path().to_str().unwrap().to_string());
        assert!(result.is_ok());
    }

    #[test]
    fn test_embed_backfill_empty_db() {
        // With --backfill and an empty traces.db, should succeed with 0 spans.
        let dir = tempfile::TempDir::new().unwrap();
        let db_path = dir.path().join("traces.db");
        let _conn = agentc_core::db::create_db(&db_path, true).unwrap();
        let result = cmd_embed(true, dir.path().to_str().unwrap().to_string());
        assert!(result.is_ok());
    }

    #[test]
    fn test_migrate_creates_fresh_db() {
        let dir = tempfile::TempDir::new().unwrap();
        let result = cmd_migrate(dir.path().to_str().unwrap().to_string());
        assert!(result.is_ok());
        // Should have created traces.db.
        assert!(dir.path().join("traces.db").exists());
    }

    #[test]
    fn test_migrate_existing_db_no_op() {
        let dir = tempfile::TempDir::new().unwrap();
        let db_path = dir.path().join("traces.db");
        let _conn = agentc_core::db::create_db(&db_path, true).unwrap();
        drop(_conn);
        // Migrate on already-current DB should succeed.
        let result = cmd_migrate(dir.path().to_str().unwrap().to_string());
        assert!(result.is_ok());
    }

}
