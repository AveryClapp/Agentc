//! Agentc CLI binary.
//!
//! Subcommands: record, traces, export.

mod helpers;

use std::collections::HashMap;
use std::env;
use std::fs;
use std::io::Write;
use std::os::unix::fs::PermissionsExt;
use std::process::{Command, ExitCode};
use std::time::{SystemTime, UNIX_EPOCH};

use clap::Parser;

use agentc_analyzer::report::{
    render_aggregate_report, render_trace_analysis, render_traces_list, AgentBreakdown,
    AggregateReport, CallRow, ModelBreakdown, TraceReport, TracesList, TracesListRow,
    WasteDetectorSummary,
};
use agentc_analyzer::{cost, waste};
use helpers::{
    extract_agent_name, most_recent_trace_id, open_canonical_db_if_exists,
    parse_since_date, resolve_storage_path, resolve_trace_id_prefix, try_merge_pending,
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

    // Capture session start time so the post-exit summary can find the trace
    // produced by *this* invocation (vs. stale traces from prior sessions).
    let session_start_us = now_micros();

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

    // Post-exit: merge pending per-process DBs, analyze the just-recorded trace,
    // and print a compact summary. Non-fatal — the child already exited, so we
    // never want to mask its exit code on summary failure.
    if let Err(e) = post_record_summary(&storage_path, session_start_us) {
        eprintln!("WARN: agentc post-record summary failed: {e}");
    }

    Ok(ExitCode::from(exit_code))
}

/// Current wall-clock time in microseconds since the Unix epoch.
fn now_micros() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_micros() as i64)
        .unwrap_or(0)
}

/// Post-exit hook: merge pending per-process DBs, find the trace from this
/// recording session, and render a compact summary.
///
/// `session_start_us` is the wall-clock time captured just before the child
/// was spawned. Traces with `start_time >= session_start_us` are considered
/// to belong to this session; we pick the earliest one (the first trace the
/// child produced).
fn post_record_summary(storage_path: &str, session_start_us: i64) -> anyhow::Result<()> {
    let storage_dir = resolve_storage_path(storage_path);

    // 1. Merge pending per-process DBs and announce if anything was merged.
    #[cfg(unix)]
    {
        match agentc_core::merge::merge_all_pending() {
            Ok(stats) if stats.spans_merged > 0 => {
                eprintln!("Merged {} spans into canonical store.", stats.spans_merged);
            }
            Ok(_) => {}
            Err(e) => {
                eprintln!("WARN: merge of pending DBs failed: {e}");
            }
        }
    }

    // 2. Open the canonical DB. Missing means the child never produced a trace.
    let Some(conn) = open_canonical_db_if_exists(&storage_dir)? else {
        return Ok(());
    };

    // 3. Find the earliest trace started during this session.
    let traces = agentc_core::db::query_traces(&conn, 50, Some(session_start_us))?;
    let Some(primary) = traces.last() else {
        // No traces from this session (e.g. child errored before any LLM call).
        return Ok(());
    };

    // 4. Backfill cost, build the analysis report, render it.
    cost::full_cost_backfill(&conn)?;
    let report = build_trace_report(&conn, &primary.trace_id)?;

    eprintln!();
    print!("{}", render_trace_analysis(&report));

    Ok(())
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
    storage_path: String,
) -> anyhow::Result<()> {
    // Validate trace ID prefix upfront (even without a DB).
    if let Some(ref tid) = trace_id {
        if tid.len() < 4 {
            anyhow::bail!(
                "Trace ID prefix must be at least 4 characters, got: '{}'",
                tid
            );
        }
    }

    let storage_dir = resolve_storage_path(&storage_path);
    try_merge_pending();

    let Some(conn) = open_canonical_db_if_exists(&storage_dir)? else {
        anyhow::bail!(
            "No traces database found at {}. Run an agent under `agentc record` first.",
            storage_dir.display()
        );
    };

    cost::full_cost_backfill(&conn)?;

    let full_trace_id = match trace_id {
        Some(prefix) => resolve_trace_id_prefix(&conn, &prefix)?,
        None => most_recent_trace_id(&conn)?
            .ok_or_else(|| anyhow::anyhow!("No traces found in storage."))?,
    };

    let report = build_trace_report(&conn, &full_trace_id)?;
    print!("{}", render_trace_analysis(&report));
    Ok(())
}

/// Build a display-ready `TraceReport` for a single trace.
///
/// - Filters the call breakdown to `kind == "chat"` spans (the LLM calls).
/// - Header totals (span count, tokens, cost, duration) span all span kinds.
/// - Waste flag span IDs are mapped to 1-based call indices so the waste
///   report can reference them.
fn build_trace_report(
    conn: &rusqlite::Connection,
    trace_id: &str,
) -> anyhow::Result<TraceReport> {
    let spans = agentc_core::db::query_spans_by_trace(conn, trace_id)?;
    if spans.is_empty() {
        anyhow::bail!("No spans found for trace '{}'.", trace_id);
    }

    // Header aggregates over all spans.
    let mut total_input: i64 = 0;
    let mut total_output: i64 = 0;
    let mut total_cost: f64 = 0.0;
    let mut min_start: i64 = i64::MAX;
    let mut max_end: i64 = 0;
    for s in &spans {
        total_input += s.input_tokens.unwrap_or(0);
        total_output += s.output_tokens.unwrap_or(0);
        total_cost += s.cost_usd.unwrap_or(0.0);
        min_start = min_start.min(s.start_time);
        if let Some(end) = s.end_time {
            max_end = max_end.max(end);
        }
    }
    let duration_us = if max_end > min_start {
        max_end - min_start
    } else {
        0
    };

    // Agent name from the root span (parent_span_id IS NULL) if present.
    let agent_name = spans
        .iter()
        .find(|s| s.parent_span_id.is_none())
        .and_then(|s| extract_agent_name(&s.attributes));

    // Call breakdown: chat spans only, 1-based indexed by start_time order.
    let chat_spans: Vec<&agentc_core::span::Span> =
        spans.iter().filter(|s| s.kind == "chat").collect();

    let mut span_id_to_call_idx: HashMap<String, usize> = HashMap::new();
    for (i, s) in chat_spans.iter().enumerate() {
        span_id_to_call_idx.insert(s.span_id.clone(), i + 1);
    }

    // Run waste detection before building call rows so we can attach flag labels.
    let waste_analysis = waste::analyze_trace(conn, trace_id)?;

    // Map span_id → list of detectors flagging that span.
    let mut span_flags: HashMap<&str, Vec<&str>> = HashMap::new();
    for flag in &waste_analysis.flags {
        for sid in &flag.span_ids {
            span_flags
                .entry(sid.as_str())
                .or_default()
                .push(flag.detector.as_str());
        }
    }

    let calls: Vec<CallRow> = chat_spans
        .iter()
        .enumerate()
        .map(|(i, s)| {
            let flag_labels = span_flags
                .get(s.span_id.as_str())
                .map(|v| v.iter().map(|d| d.to_string()).collect())
                .unwrap_or_default();
            CallRow {
                index: i + 1,
                agent: extract_agent_name(&s.attributes),
                model: s.model.clone(),
                input_tokens: s.input_tokens.unwrap_or(0),
                output_tokens: s.output_tokens.unwrap_or(0),
                cost_usd: s.cost_usd,
                flag_labels,
            }
        })
        .collect();

    Ok(TraceReport {
        trace_id: trace_id.to_string(),
        agent_name,
        duration_us,
        span_count: spans.len(),
        total_input_tokens: total_input,
        total_output_tokens: total_output,
        total_cost_usd: total_cost,
        calls,
        waste: waste_analysis,
        span_id_to_call_idx,
    })
}

fn cmd_report(
    last: Option<usize>,
    since: Option<String>,
    agent: Option<String>,
    model: Option<String>,
    storage_path: String,
) -> anyhow::Result<()> {
    let storage_dir = resolve_storage_path(&storage_path);
    try_merge_pending();

    let Some(conn) = open_canonical_db_if_exists(&storage_dir)? else {
        print!("{}", render_aggregate_report(&AggregateReport::default()));
        return Ok(());
    };

    cost::full_cost_backfill(&conn)?;

    let since_us = match since {
        Some(ref s) => Some(parse_since_date(s)?),
        None => None,
    };

    // --last is a trace-level cap (most recent N). Default matches the spec's 50.
    let limit = last.unwrap_or(50) as i64;
    let trace_summaries = agentc_core::db::query_traces(&conn, limit, since_us)?;

    let report = build_aggregate_report(
        &conn,
        &trace_summaries,
        agent.as_deref(),
        model.as_deref(),
    )?;
    print!("{}", render_aggregate_report(&report));
    Ok(())
}

/// Aggregate spans across the given traces into a display-ready `AggregateReport`.
///
/// `agent_filter` and `model_filter` filter at the span level: a span is counted
/// only if all provided filters match. Traces whose spans are all filtered out
/// contribute zero to the totals (but still count in `trace_count` if they had
/// any matching spans).
fn build_aggregate_report(
    conn: &rusqlite::Connection,
    trace_summaries: &[agentc_core::span::TraceSummary],
    agent_filter: Option<&str>,
    model_filter: Option<&str>,
) -> anyhow::Result<AggregateReport> {
    let mut total_tokens: i64 = 0;
    let mut total_cost: f64 = 0.0;
    let mut min_start: Option<i64> = None;
    let mut max_start: Option<i64> = None;
    let mut matched_trace_count: usize = 0;

    let mut by_model: HashMap<String, (i64, f64)> = HashMap::new();
    let mut by_agent: HashMap<String, (i64, f64)> = HashMap::new();
    let mut waste_totals: HashMap<String, (usize, f64)> = HashMap::new();

    for summary in trace_summaries {
        let spans = agentc_core::db::query_spans_by_trace(conn, &summary.trace_id)?;
        if spans.is_empty() {
            continue;
        }

        let mut matched_any = false;
        for s in &spans {
            if let Some(m) = model_filter {
                if s.model.as_deref() != Some(m) {
                    continue;
                }
            }
            let span_agent = extract_agent_name(&s.attributes);
            if let Some(a) = agent_filter {
                if span_agent.as_deref() != Some(a) {
                    continue;
                }
            }

            matched_any = true;
            let tokens = s.input_tokens.unwrap_or(0) + s.output_tokens.unwrap_or(0);
            let cost = s.cost_usd.unwrap_or(0.0);
            total_tokens += tokens;
            total_cost += cost;

            if let Some(ref m) = s.model {
                let e = by_model.entry(m.clone()).or_insert((0, 0.0));
                e.0 += tokens;
                e.1 += cost;
            }
            if let Some(a) = span_agent {
                let e = by_agent.entry(a).or_insert((0, 0.0));
                e.0 += tokens;
                e.1 += cost;
            }
        }

        if !matched_any {
            continue;
        }
        matched_trace_count += 1;
        min_start = Some(min_start.map_or(summary.start_time, |m| m.min(summary.start_time)));
        max_start = Some(max_start.map_or(summary.start_time, |m| m.max(summary.start_time)));

        // Waste analysis is trace-level; it inherits the trace-set filter only.
        let analysis = waste::analyze_trace(conn, &summary.trace_id)?;
        for flag in &analysis.flags {
            let e = waste_totals
                .entry(flag.detector.clone())
                .or_insert((0, 0.0));
            e.0 += 1;
            e.1 += flag.estimated_cost.unwrap_or(0.0);
        }
    }

    let mut by_model_vec: Vec<ModelBreakdown> = by_model
        .into_iter()
        .map(|(model, (t, c))| ModelBreakdown {
            model,
            total_tokens: t,
            total_cost_usd: c,
        })
        .collect();
    by_model_vec.sort_by(|a, b| b.total_cost_usd.partial_cmp(&a.total_cost_usd).unwrap());

    let mut by_agent_vec: Vec<AgentBreakdown> = by_agent
        .into_iter()
        .map(|(agent, (t, c))| AgentBreakdown {
            agent,
            total_tokens: t,
            total_cost_usd: c,
        })
        .collect();
    by_agent_vec.sort_by(|a, b| b.total_cost_usd.partial_cmp(&a.total_cost_usd).unwrap());

    let mut waste_vec: Vec<WasteDetectorSummary> = waste_totals
        .into_iter()
        .map(|(detector, (count, waste_usd))| WasteDetectorSummary {
            detector,
            flag_count: count,
            estimated_waste_usd: waste_usd,
        })
        .collect();
    waste_vec.sort_by(|a, b| {
        b.estimated_waste_usd
            .partial_cmp(&a.estimated_waste_usd)
            .unwrap()
    });

    Ok(AggregateReport {
        trace_count: matched_trace_count,
        date_start_us: min_start,
        date_end_us: max_start,
        total_tokens,
        total_cost_usd: total_cost,
        by_model: by_model_vec,
        by_agent: by_agent_vec,
        waste_summary: waste_vec,
    })
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
    storage_path: String,
) -> anyhow::Result<()> {
    if trace_id.len() < 4 {
        anyhow::bail!(
            "Trace ID prefix must be at least 4 characters, got: '{}'",
            trace_id
        );
    }

    let storage_dir = resolve_storage_path(&storage_path);
    try_merge_pending();

    let conn = open_canonical_db_if_exists(&storage_dir)?.ok_or_else(|| {
        anyhow::anyhow!("No traces database at {}", storage_dir.display())
    })?;

    cost::full_cost_backfill(&conn)?;
    let resolved = resolve_trace_id_prefix(&conn, &trace_id)?;
    let spans = agentc_core::db::query_spans_by_trace(&conn, &resolved)?;

    let otlp_spans: Vec<serde_json::Value> = spans.iter().map(span_to_otlp_json).collect();

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
                "spans": otlp_spans,
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

/// Convert a stored `Span` into an OTLP/HTTP JSON span object.
///
/// Column values (model, provider, tokens, cost) are promoted into `gen_ai.*`
/// attributes and override any matching keys in the stored `attributes` JSON.
fn span_to_otlp_json(span: &agentc_core::span::Span) -> serde_json::Value {
    use serde_json::Value;

    let mut attrs: serde_json::Map<String, Value> =
        match serde_json::from_str::<Value>(&span.attributes) {
            Ok(Value::Object(m)) => m,
            _ => serde_json::Map::new(),
        };

    attrs.insert(
        "gen_ai.operation.name".into(),
        Value::String(span.kind.clone()),
    );
    if let Some(ref m) = span.model {
        attrs.insert("gen_ai.response.model".into(), Value::String(m.clone()));
    }
    if let Some(ref p) = span.provider {
        attrs.insert("gen_ai.provider.name".into(), Value::String(p.clone()));
    }
    if let Some(t) = span.input_tokens {
        attrs.insert("gen_ai.usage.input_tokens".into(), Value::from(t));
    }
    if let Some(t) = span.output_tokens {
        attrs.insert("gen_ai.usage.output_tokens".into(), Value::from(t));
    }
    if let Some(t) = span.cache_creation_tokens {
        attrs.insert(
            "gen_ai.usage.cache_creation.input_tokens".into(),
            Value::from(t),
        );
    }
    if let Some(t) = span.cache_read_tokens {
        attrs.insert(
            "gen_ai.usage.cache_read.input_tokens".into(),
            Value::from(t),
        );
    }
    if let Some(c) = span.cost_usd {
        if let Some(n) = serde_json::Number::from_f64(c) {
            attrs.insert("agentc.cost_usd".into(), Value::Number(n));
        }
    }

    let otlp_attrs: Vec<Value> = attrs
        .into_iter()
        .map(|(k, v)| json_to_otlp_attr(k, v))
        .collect();

    // OTel SpanKind enum: CHAT spans are CLIENT (3) — outbound LLM API calls.
    // Internal work (tool execution, agent invocation) is INTERNAL (1).
    let otel_kind = if span.kind == "chat" { 3 } else { 1 };
    let status_code = match span.status.as_str() {
        "OK" => 1,
        "ERROR" => 2,
        _ => 0,
    };

    let mut span_obj = serde_json::json!({
        "traceId": span.trace_id,
        "spanId": span.span_id,
        "name": span.name,
        "kind": otel_kind,
        "startTimeUnixNano": (span.start_time * 1000).to_string(),
        "attributes": otlp_attrs,
        "status": { "code": status_code },
    });
    if let Some(end) = span.end_time {
        span_obj["endTimeUnixNano"] =
            serde_json::Value::String((end * 1000).to_string());
    }
    if let Some(ref p) = span.parent_span_id {
        span_obj["parentSpanId"] = serde_json::Value::String(p.clone());
    }
    span_obj
}

/// Wrap a JSON leaf into an OTLP AnyValue. Complex values are JSON-stringified.
fn json_to_otlp_attr(key: String, value: serde_json::Value) -> serde_json::Value {
    use serde_json::Value;
    let otlp_value = match &value {
        Value::String(s) => serde_json::json!({ "stringValue": s }),
        Value::Bool(b) => serde_json::json!({ "boolValue": b }),
        Value::Number(n) => {
            if let Some(i) = n.as_i64() {
                serde_json::json!({ "intValue": i.to_string() })
            } else if let Some(f) = n.as_f64() {
                serde_json::json!({ "doubleValue": f })
            } else {
                serde_json::json!({ "stringValue": n.to_string() })
            }
        }
        Value::Null => serde_json::json!({ "stringValue": "" }),
        _ => serde_json::json!({
            "stringValue": serde_json::to_string(&value).unwrap_or_default(),
        }),
    };
    serde_json::json!({ "key": key, "value": otlp_value })
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
        let dir = tempfile::TempDir::new().unwrap();
        let result = cmd_export(
            "abc".to_string(),
            None,
            dir.path().to_string_lossy().into_owned(),
        );
        assert!(result.is_err());
        let err = result.unwrap_err().to_string();
        assert!(err.contains("at least 4 characters"));
    }

    #[test]
    fn test_export_no_db() {
        let dir = tempfile::TempDir::new().unwrap();
        let result = cmd_export(
            "abcd1234".to_string(),
            None,
            dir.path().to_string_lossy().into_owned(),
        );
        assert!(result.is_err());
        assert!(result.unwrap_err().to_string().contains("No traces database"));
    }

    #[test]
    fn test_export_no_match() {
        let dir = make_test_dir_with_one_trace("abcd1234efgh");
        let result = cmd_export(
            "zzzz".to_string(),
            None,
            dir.path().to_string_lossy().into_owned(),
        );
        assert!(result.is_err());
        assert!(result.unwrap_err().to_string().contains("No trace found"));
    }

    #[test]
    fn test_export_stdout() {
        let dir = make_test_dir_with_one_trace("abcd1234efgh");
        let result = cmd_export(
            "abcd".to_string(),
            None,
            dir.path().to_string_lossy().into_owned(),
        );
        assert!(result.is_ok(), "got error: {:?}", result.err());
    }

    #[test]
    fn test_export_to_file() {
        let dir = make_test_dir_with_one_trace("abcd1234efgh");
        let out_path = dir.path().join("trace.json");
        let result = cmd_export(
            "abcd".to_string(),
            Some(out_path.to_string_lossy().into_owned()),
            dir.path().to_string_lossy().into_owned(),
        );
        assert!(result.is_ok(), "got error: {:?}", result.err());
        assert!(out_path.exists());

        let contents = std::fs::read_to_string(&out_path).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&contents).unwrap();
        let spans = &parsed["resourceSpans"][0]["scopeSpans"][0]["spans"];
        assert!(spans.is_array());
        assert_eq!(spans.as_array().unwrap().len(), 1);
        let span = &spans[0];
        assert_eq!(span["traceId"], "abcd1234efgh");
        assert_eq!(span["spanId"], "span-1");
        assert_eq!(span["kind"], 3); // chat → CLIENT

        // Verify promoted gen_ai attributes made it into the attribute list.
        let attrs = span["attributes"].as_array().unwrap();
        let keys: Vec<&str> = attrs.iter().map(|a| a["key"].as_str().unwrap()).collect();
        assert!(keys.contains(&"gen_ai.operation.name"));
        assert!(keys.contains(&"gen_ai.response.model"));
        assert!(keys.contains(&"gen_ai.usage.input_tokens"));
        assert!(keys.contains(&"gen_ai.agent.name"));
    }

    #[test]
    fn test_span_to_otlp_json_all_value_types() {
        // Parent span, end_time, cost, cache tokens exercised.
        let span = agentc_core::span::Span {
            span_id: "s2".to_string(),
            trace_id: "t2".to_string(),
            parent_span_id: Some("s1".to_string()),
            name: "child".to_string(),
            kind: "execute_tool".to_string(),
            start_time: 1_000,
            end_time: Some(2_000),
            status: "ERROR".to_string(),
            model: Some("claude-haiku-4-5".to_string()),
            provider: Some("anthropic".to_string()),
            input_tokens: Some(10),
            output_tokens: Some(5),
            cache_creation_tokens: Some(1),
            cache_read_tokens: Some(2),
            cost_usd: Some(0.0123),
            attributes: r#"{"custom.bool": true, "custom.list": [1,2,3]}"#.to_string(),
            input_content_id: None,
            output_content_id: None,
            input_embedding: None,
            output_embedding: None,
            embedding_model: None,
        };
        let json = span_to_otlp_json(&span);
        assert_eq!(json["kind"], 1); // execute_tool → INTERNAL
        assert_eq!(json["status"]["code"], 2); // ERROR
        assert_eq!(json["parentSpanId"], "s1");
        assert_eq!(json["startTimeUnixNano"], "1000000"); // us → ns
        assert_eq!(json["endTimeUnixNano"], "2000000");

        let attrs = json["attributes"].as_array().unwrap();
        // bool and array types round-trip correctly.
        let bool_attr = attrs
            .iter()
            .find(|a| a["key"] == "custom.bool")
            .expect("custom.bool missing");
        assert_eq!(bool_attr["value"]["boolValue"], true);
        let list_attr = attrs
            .iter()
            .find(|a| a["key"] == "custom.list")
            .expect("custom.list missing");
        assert!(list_attr["value"]["stringValue"].is_string());

        let cost_attr = attrs
            .iter()
            .find(|a| a["key"] == "agentc.cost_usd")
            .expect("cost missing");
        assert!(cost_attr["value"]["doubleValue"].is_f64());
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
    fn test_analyze_short_prefix_fails() {
        // Validation happens before any DB access.
        let dir = tempfile::TempDir::new().unwrap();
        let result = cmd_analyze(Some("ab".to_string()), dir.path().to_string_lossy().into_owned());
        assert!(result.is_err());
        assert!(result.unwrap_err().to_string().contains("at least 4"));
    }

    #[test]
    fn test_analyze_no_db() {
        let dir = tempfile::TempDir::new().unwrap();
        let result = cmd_analyze(None, dir.path().to_string_lossy().into_owned());
        assert!(result.is_err());
        assert!(result.unwrap_err().to_string().contains("No traces database"));
    }

    #[test]
    fn test_analyze_empty_db() {
        let dir = tempfile::TempDir::new().unwrap();
        let db_path = dir.path().join("traces.db");
        let _conn = agentc_core::db::create_db(&db_path, true).unwrap();

        let result = cmd_analyze(None, dir.path().to_string_lossy().into_owned());
        assert!(result.is_err());
        assert!(result.unwrap_err().to_string().contains("No traces found"));
    }

    #[test]
    fn test_analyze_prefix_no_match() {
        let dir = make_test_dir_with_one_trace("abcd1234efgh");
        let result = cmd_analyze(
            Some("zzzz".to_string()),
            dir.path().to_string_lossy().into_owned(),
        );
        assert!(result.is_err());
        assert!(result.unwrap_err().to_string().contains("No trace found"));
    }

    #[test]
    fn test_analyze_with_data() {
        let dir = make_test_dir_with_one_trace("abcd1234efgh");
        let result = cmd_analyze(
            Some("abcd".to_string()),
            dir.path().to_string_lossy().into_owned(),
        );
        assert!(result.is_ok(), "got error: {:?}", result.err());
    }

    #[test]
    fn test_analyze_most_recent() {
        let dir = make_test_dir_with_one_trace("abcd1234efgh");
        let result = cmd_analyze(None, dir.path().to_string_lossy().into_owned());
        assert!(result.is_ok(), "got error: {:?}", result.err());
    }

    /// Create a tempdir with a traces.db containing one chat span on the given trace ID.
    fn make_test_dir_with_one_trace(trace_id: &str) -> tempfile::TempDir {
        let dir = tempfile::TempDir::new().unwrap();
        let db_path = dir.path().join("traces.db");
        let conn = agentc_core::db::create_db(&db_path, true).unwrap();
        let span = agentc_core::span::Span {
            span_id: "span-1".to_string(),
            trace_id: trace_id.to_string(),
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
            attributes: r#"{"gen_ai.agent.name": "review-agent"}"#.to_string(),
            input_content_id: None,
            output_content_id: None,
            input_embedding: None,
            output_embedding: None,
            embedding_model: None,
        };
        agentc_core::db::insert_span(&conn, &span).unwrap();
        drop(conn);
        dir
    }

    #[test]
    fn test_report_empty_storage() {
        // No traces.db at path — should render an empty aggregate report, not error.
        let dir = tempfile::TempDir::new().unwrap();
        let result = cmd_report(
            Some(10),
            None,
            None,
            None,
            dir.path().to_string_lossy().into_owned(),
        );
        assert!(result.is_ok(), "got error: {:?}", result.err());
    }

    #[test]
    fn test_report_with_data() {
        let dir = make_test_dir_with_one_trace("abcd1234efgh");
        let result = cmd_report(
            Some(10),
            None,
            None,
            None,
            dir.path().to_string_lossy().into_owned(),
        );
        assert!(result.is_ok(), "got error: {:?}", result.err());
    }

    #[test]
    fn test_report_agent_filter_match() {
        let dir = make_test_dir_with_one_trace("abcd1234efgh");
        let result = cmd_report(
            Some(10),
            None,
            Some("review-agent".to_string()),
            None,
            dir.path().to_string_lossy().into_owned(),
        );
        assert!(result.is_ok(), "got error: {:?}", result.err());
    }

    #[test]
    fn test_report_agent_filter_no_match() {
        let dir = make_test_dir_with_one_trace("abcd1234efgh");
        let result = cmd_report(
            Some(10),
            None,
            Some("nonexistent-agent".to_string()),
            None,
            dir.path().to_string_lossy().into_owned(),
        );
        assert!(result.is_ok(), "got error: {:?}", result.err());
    }

    #[test]
    fn test_report_model_filter() {
        let dir = make_test_dir_with_one_trace("abcd1234efgh");
        let result = cmd_report(
            Some(10),
            None,
            None,
            Some("claude-sonnet-4-20250514".to_string()),
            dir.path().to_string_lossy().into_owned(),
        );
        assert!(result.is_ok(), "got error: {:?}", result.err());
    }

    #[test]
    fn test_report_since_filter() {
        let dir = make_test_dir_with_one_trace("abcd1234efgh");
        let result = cmd_report(
            Some(10),
            Some("2020-01-01".to_string()),
            None,
            None,
            dir.path().to_string_lossy().into_owned(),
        );
        assert!(result.is_ok(), "got error: {:?}", result.err());
    }

    #[test]
    fn test_report_invalid_since_date() {
        let dir = make_test_dir_with_one_trace("abcd1234efgh");
        let result = cmd_report(
            Some(10),
            Some("not-a-date".to_string()),
            None,
            None,
            dir.path().to_string_lossy().into_owned(),
        );
        assert!(result.is_err());
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

    #[test]
    fn test_post_record_summary_missing_storage_noop() {
        // Fresh temp dir with no traces.db at all — summary should be a no-op.
        let dir = tempfile::TempDir::new().unwrap();
        let result = post_record_summary(
            dir.path().to_str().unwrap(),
            0, // session start — irrelevant since no DB exists
        );
        assert!(result.is_ok());
    }

    #[test]
    fn test_post_record_summary_with_fresh_trace() {
        // Storage has one trace whose start_time is > session_start_us —
        // summary should find it and render successfully.
        let dir = make_test_dir_with_one_trace("sess1234efgh");
        // Trace start_time is 1_000_000_000_000_000 — pick a session start strictly before it.
        let result = post_record_summary(
            dir.path().to_str().unwrap(),
            999_999_999_999_999,
        );
        assert!(result.is_ok());
    }

    #[test]
    fn test_post_record_summary_no_session_traces() {
        // DB has traces but all pre-date session_start_us — summary is a no-op.
        let dir = make_test_dir_with_one_trace("old1234efgh");
        // Trace start_time is 1_000_000_000_000_000 — pick a session start strictly after it.
        let result = post_record_summary(
            dir.path().to_str().unwrap(),
            2_000_000_000_000_000,
        );
        assert!(result.is_ok());
    }

    #[test]
    fn test_now_micros_monotonic() {
        let a = now_micros();
        let b = now_micros();
        assert!(b >= a);
        assert!(a > 0);
    }
}
