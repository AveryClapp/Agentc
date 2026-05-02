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
    /// Inspect and manage the memoization cache.
    Cache {
        #[command(subcommand)]
        cmd: CacheCmd,
    },
    /// Inspect and manage the JIT optimizer.
    Optimize {
        #[command(subcommand)]
        cmd: OptimizeCmd,
    },
}

#[derive(clap::Subcommand)]
enum OptimizeCmd {
    /// Aggregate report over the last N hours of optimizer activity.
    Report {
        /// Window size in hours (default 24).
        #[arg(long, default_value_t = 24)]
        hours: u64,
        /// Storage path.
        #[arg(long, default_value = "~/.agentc")]
        storage_path: String,
    },
    /// Per-call-site cost model + rule firing rates + accuracy status.
    Inspect {
        /// Target call_site_id.
        call_site: String,
        /// Storage path.
        #[arg(long, default_value = "~/.agentc")]
        storage_path: String,
    },
    /// Operator override: write `optimizer_disabled` rows.
    Disable {
        /// Rule name to disable (e.g. ModelDowngrade).
        #[arg(long)]
        rule: String,
        /// Call-site GLOB (`*`, `?`).
        #[arg(long = "call-site")]
        call_site: String,
        /// Reason recorded in the disable row (default "operator override").
        #[arg(long, default_value = "operator override")]
        reason: String,
        /// Cooldown in hours before the rule re-enables (default 24h).
        #[arg(long, default_value_t = 24)]
        hours: u64,
        /// Storage path.
        #[arg(long, default_value = "~/.agentc")]
        storage_path: String,
    },
    /// Run an agent twice (optimizer off / on) and diff the results.
    Bench {
        /// Path to the agent script (passed to `python <agent>`).
        #[arg(long)]
        agent: String,
        /// Storage path.
        #[arg(long, default_value = "~/.agentc")]
        storage_path: String,
    },
}

#[derive(clap::Subcommand)]
enum CacheCmd {
    /// Summary: entries, hit/miss breakdown, savings, top call sites.
    Stats {
        /// Window in hours for the hit/miss breakdown (default 24).
        #[arg(long, default_value_t = 24)]
        hours: u64,
        /// Storage path.
        #[arg(long, default_value = "~/.agentc")]
        storage_path: String,
    },
    /// Show one cache entry by `cache_key_hash` prefix (>= 4 chars).
    Inspect {
        /// cache_key_hash prefix (min 4 chars).
        prefix: String,
        /// Storage path.
        #[arg(long, default_value = "~/.agentc")]
        storage_path: String,
    },
    /// Drop cache entries. Exactly one filter is required.
    Evict {
        /// Drop entries older than this duration (e.g. "7d", "12h", "30m").
        #[arg(long, conflicts_with_all = ["pattern", "all"])]
        older_than: Option<String>,
        /// Drop entries whose call_site_id matches this GLOB (e.g. "app.*").
        #[arg(long, conflicts_with_all = ["older_than", "all"])]
        pattern: Option<String>,
        /// Drop every entry.
        #[arg(long, conflicts_with_all = ["older_than", "pattern"])]
        all: bool,
        /// Storage path.
        #[arg(long, default_value = "~/.agentc")]
        storage_path: String,
    },
    /// Compare a call site's cost with/without memoization.
    Bench {
        /// Target call_site_id.
        #[arg(long)]
        call_site: String,
        /// Number of runs (default 100).
        #[arg(long, default_value_t = 100)]
        runs: u64,
        /// Shadow mode: compute divergence between baseline and memoized output.
        #[arg(long)]
        shadow: bool,
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

    // The merge helpers in agentc-core read AGENTC_STORAGE_PATH to locate the
    // per-process active/ dir and the canonical traces.db. The parent CLI
    // process inherits whatever the user's shell set; for `agentc record
    // --storage-path X` we have to point it at X explicitly so the merge
    // lands in the same dir we just told the child to write to.
    // SAFETY: process is single-threaded at this point — child has exited.
    unsafe {
        std::env::set_var("AGENTC_STORAGE_PATH", storage_path);
    }

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
        Cli::Cache { cmd } => {
            cmd_cache(cmd)?;
            Ok(ExitCode::SUCCESS)
        }
        Cli::Optimize { cmd } => {
            cmd_optimize(cmd)?;
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

// ---------------------------------------------------------------------------
// agentc cache
// ---------------------------------------------------------------------------

fn cmd_cache(cmd: CacheCmd) -> anyhow::Result<()> {
    match cmd {
        CacheCmd::Stats { hours, storage_path } => cmd_cache_stats(hours, storage_path),
        CacheCmd::Inspect { prefix, storage_path } => cmd_cache_inspect(prefix, storage_path),
        CacheCmd::Evict {
            older_than,
            pattern,
            all,
            storage_path,
        } => cmd_cache_evict(older_than, pattern, all, storage_path),
        CacheCmd::Bench {
            call_site,
            runs,
            shadow,
            storage_path,
        } => cmd_cache_bench(call_site, runs, shadow, storage_path),
    }
}

fn open_canonical_for_cache(storage_path: &str) -> anyhow::Result<(rusqlite::Connection, std::path::PathBuf)> {
    let storage_dir = resolve_storage_path(storage_path);
    try_merge_pending();
    let db_path = storage_dir.join("traces.db");
    if !db_path.exists() {
        anyhow::bail!(
            "No traces database found at {}. Nothing cached yet.",
            db_path.display()
        );
    }
    let conn = agentc_core::db::open_db(&db_path)?;
    agentc_memo::schema::ensure_schema(&conn)?;
    Ok((conn, db_path))
}

fn format_usd(v: f64) -> String {
    format!("${v:.2}")
}

fn format_count(n: u64) -> String {
    // "1,234,567" — thousands separator, no SI prefix (CLI mockup style).
    let s = n.to_string();
    let mut out = String::with_capacity(s.len() + s.len() / 3);
    for (i, c) in s.chars().rev().enumerate() {
        if i > 0 && i % 3 == 0 {
            out.push(',');
        }
        out.push(c);
    }
    out.chars().rev().collect()
}

fn format_bytes_human(n: u64) -> String {
    const KB: f64 = 1024.0;
    const MB: f64 = 1024.0 * 1024.0;
    const GB: f64 = 1024.0 * 1024.0 * 1024.0;
    let f = n as f64;
    if f >= GB {
        format!("{:.1} GB", f / GB)
    } else if f >= MB {
        format!("{:.1} MB", f / MB)
    } else if f >= KB {
        format!("{:.1} KB", f / KB)
    } else {
        format!("{n} B")
    }
}

fn format_unix_micros(us: i64) -> String {
    // Rough UTC ISO-8601, seconds precision, no external time crate.
    let secs_total = (us / 1_000_000).max(0);
    let (y, mo, d, h, mi, s) = civil_from_epoch_seconds(secs_total);
    format!("{y:04}-{mo:02}-{d:02} {h:02}:{mi:02}:{s:02} UTC")
}

/// Unix seconds → (year, month, day, hour, minute, second) in UTC.
/// Inverse of Howard Hinnant's civil_to_days algorithm already used in helpers.rs.
fn civil_from_epoch_seconds(s: i64) -> (i32, u32, u32, u32, u32, u32) {
    let days = s.div_euclid(86_400);
    let rem = s.rem_euclid(86_400) as u32;
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097) as u64;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let y = yoe as i64 + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = if m <= 2 { y + 1 } else { y };
    let hour = rem / 3600;
    let mi = (rem / 60) % 60;
    let sec = rem % 60;
    (y as i32, m as u32, d as u32, hour, mi, sec)
}

/// Parse durations like "7d", "12h", "30m", "45s". Returns microseconds.
fn parse_duration_micros(s: &str) -> anyhow::Result<i64> {
    let s = s.trim();
    if s.is_empty() {
        anyhow::bail!("empty duration");
    }
    let (num_str, unit) = s.split_at(s.len() - 1);
    let value: i64 = num_str
        .parse()
        .map_err(|_| anyhow::anyhow!("bad duration '{}'; expected <number><s|m|h|d>", s))?;
    let multiplier: i64 = match unit {
        "s" => 1_000_000,
        "m" => 60 * 1_000_000,
        "h" => 3_600 * 1_000_000,
        "d" => 86_400 * 1_000_000,
        _ => anyhow::bail!("unknown duration unit '{}' (use s|m|h|d)", unit),
    };
    value
        .checked_mul(multiplier)
        .ok_or_else(|| anyhow::anyhow!("duration overflow: {}", s))
}

/// Cache hit/miss breakdown derived from spans tagged by the `@memoize`
/// decorator. The decorator emits span attributes `agentc.cache.result` in
/// {"exact","lsh","miss"} and `agentc.cache.saved_cost_usd` on hits. Older
/// spans without these attributes are ignored.
struct HitBreakdown {
    exact: u64,
    lsh: u64,
    miss: u64,
    saved_cost_usd: f64,
    saved_tokens: i64,
    p99_lookup_ms: Option<f64>,
    exact_median_ms: Option<f64>,
    lsh_median_ms: Option<f64>,
}

fn query_hit_breakdown(conn: &rusqlite::Connection, window_micros: i64) -> anyhow::Result<HitBreakdown> {
    let cutoff = now_micros().saturating_sub(window_micros);
    let mut stmt = conn.prepare(
        "SELECT attributes, COALESCE(end_time, start_time) - start_time AS lat_us \
         FROM spans WHERE start_time >= ?1 AND kind = 'chat'",
    )?;
    let mut exact = 0u64;
    let mut lsh = 0u64;
    let mut miss = 0u64;
    let mut saved_cost = 0.0f64;
    let mut saved_tokens = 0i64;
    let mut lat_exact_ms: Vec<f64> = Vec::new();
    let mut lat_lsh_ms: Vec<f64> = Vec::new();
    let mut lat_all_ms: Vec<f64> = Vec::new();
    let rows = stmt.query_map(rusqlite::params![cutoff], |r| {
        let attrs: String = r.get(0)?;
        let lat_us: i64 = r.get(1)?;
        Ok((attrs, lat_us))
    })?;
    for row in rows {
        let (attrs_json, lat_us) = row?;
        let Ok(val) = serde_json::from_str::<serde_json::Value>(&attrs_json) else {
            continue;
        };
        let Some(result) = val.get("agentc.cache.result").and_then(|v| v.as_str()) else {
            continue;
        };
        let lat_ms = lat_us as f64 / 1000.0;
        lat_all_ms.push(lat_ms);
        match result {
            "exact" => {
                exact += 1;
                lat_exact_ms.push(lat_ms);
            }
            "lsh" => {
                lsh += 1;
                lat_lsh_ms.push(lat_ms);
            }
            "miss" => miss += 1,
            _ => continue,
        }
        if let Some(c) = val.get("agentc.cache.saved_cost_usd").and_then(|v| v.as_f64()) {
            saved_cost += c;
        }
        if let Some(t) = val.get("agentc.cache.saved_tokens").and_then(|v| v.as_i64()) {
            saved_tokens += t;
        }
    }
    Ok(HitBreakdown {
        exact,
        lsh,
        miss,
        saved_cost_usd: saved_cost,
        saved_tokens,
        p99_lookup_ms: percentile(&mut lat_all_ms, 0.99),
        exact_median_ms: percentile(&mut lat_exact_ms, 0.50),
        lsh_median_ms: percentile(&mut lat_lsh_ms, 0.50),
    })
}

fn percentile(samples: &mut [f64], p: f64) -> Option<f64> {
    if samples.is_empty() {
        return None;
    }
    samples.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let idx = ((samples.len() as f64 - 1.0) * p).round() as usize;
    samples.get(idx).copied()
}

fn cmd_cache_stats(hours: u64, storage_path: String) -> anyhow::Result<()> {
    let (conn, db_path) = open_canonical_for_cache(&storage_path)?;
    let stats = agentc_memo::ffi::stats(&conn);
    let window_us = (hours as i64).saturating_mul(3_600 * 1_000_000);
    let hits = query_hit_breakdown(&conn, window_us)?;

    let total_ops = hits.exact + hits.lsh + hits.miss;
    let pct = |n: u64| -> String {
        if total_ops == 0 {
            "—".to_string()
        } else {
            format!("{:.1}%", (n as f64 / total_ops as f64) * 100.0)
        }
    };

    let on_disk = db_size_bytes(&db_path).unwrap_or(stats.bytes_on_disk);

    println!("Cache summary (last {hours}h)");
    println!("─────────────────────────────────────────────────────────");
    println!(
        "Entries:          {:<10} ({} on disk)",
        format_count(stats.entries),
        format_bytes_human(on_disk)
    );
    println!("Exact hits:       {:<10} ({})", format_count(hits.exact), pct(hits.exact));
    println!("LSH hits:         {:<10} ({})", format_count(hits.lsh), pct(hits.lsh));
    println!("Misses:           {:<10} ({})", format_count(hits.miss), pct(hits.miss));
    println!();
    println!(
        "Savings:          {:<10} {} tokens",
        format_usd(hits.saved_cost_usd),
        format_count(hits.saved_tokens.max(0) as u64)
    );
    match (hits.p99_lookup_ms, hits.exact_median_ms, hits.lsh_median_ms) {
        (Some(p99), Some(e), Some(l)) => println!(
            "p99 lookup:       {:.1}ms      (exact {:.1}ms, lsh {:.1}ms)",
            p99, e, l
        ),
        (Some(p99), Some(e), None) => {
            println!("p99 lookup:       {:.1}ms      (exact {:.1}ms)", p99, e)
        }
        (Some(p99), None, _) => println!("p99 lookup:       {:.1}ms", p99),
        _ => println!("p99 lookup:       —"),
    }

    // Top call sites by hit rate, from span-level agentc.cache.result.
    let top = query_top_call_sites(&conn, window_us, 4)?;
    if !top.is_empty() {
        println!();
        println!("Top call sites by hit rate:");
        for row in top {
            println!("  {:<40} {:.1}% hit", row.call_site, row.hit_rate * 100.0);
        }
    }
    Ok(())
}

fn db_size_bytes(path: &std::path::Path) -> Option<u64> {
    std::fs::metadata(path).ok().map(|m| m.len())
}

struct TopCallSite {
    call_site: String,
    hit_rate: f64,
}

struct CacheEntryRow {
    key: String,
    site: String,
    model: String,
    hits: i64,
    created: i64,
    last_hit: i64,
    expires: i64,
    in_tok: i64,
    out_tok: i64,
    content_id: String,
}

fn query_top_call_sites(
    conn: &rusqlite::Connection,
    window_micros: i64,
    limit: usize,
) -> anyhow::Result<Vec<TopCallSite>> {
    let cutoff = now_micros().saturating_sub(window_micros);
    let mut stmt = conn.prepare(
        "SELECT attributes FROM spans WHERE start_time >= ?1 AND kind = 'chat'",
    )?;
    let mut by_site: HashMap<String, (u64, u64)> = HashMap::new(); // site → (hits, total)
    let rows = stmt.query_map(rusqlite::params![cutoff], |r| {
        let s: String = r.get(0)?;
        Ok(s)
    })?;
    for row in rows {
        let attrs_json = row?;
        let Ok(val) = serde_json::from_str::<serde_json::Value>(&attrs_json) else {
            continue;
        };
        let Some(site) = val.get("agentc.cache.call_site").and_then(|v| v.as_str()) else {
            continue;
        };
        let Some(result) = val.get("agentc.cache.result").and_then(|v| v.as_str()) else {
            continue;
        };
        let entry = by_site.entry(site.to_string()).or_insert((0, 0));
        entry.1 += 1;
        if matches!(result, "exact" | "lsh") {
            entry.0 += 1;
        }
    }
    let mut ranked: Vec<TopCallSite> = by_site
        .into_iter()
        .filter(|(_, (_, total))| *total > 0)
        .map(|(call_site, (hits, total))| TopCallSite {
            call_site,
            hit_rate: hits as f64 / total as f64,
        })
        .collect();
    ranked.sort_by(|a, b| b.hit_rate.partial_cmp(&a.hit_rate).unwrap_or(std::cmp::Ordering::Equal));
    ranked.truncate(limit);
    Ok(ranked)
}

fn cmd_cache_inspect(prefix: String, storage_path: String) -> anyhow::Result<()> {
    if prefix.len() < 4 {
        anyhow::bail!(
            "cache_key_hash prefix must be at least 4 characters, got '{}'",
            prefix
        );
    }
    let (conn, _) = open_canonical_for_cache(&storage_path)?;

    let like = format!("{prefix}%");
    let mut stmt = conn.prepare(
        "SELECT cache_key_hash, call_site_id, model, hit_count, \
                created_at, last_hit_at, expires_at, \
                input_tokens, output_tokens, output_content_id \
         FROM memoization_cache WHERE cache_key_hash LIKE ?1 LIMIT 10",
    )?;
    let rows: Vec<CacheEntryRow> = stmt
        .query_map(rusqlite::params![like], |r| {
            Ok(CacheEntryRow {
                key: r.get::<_, String>(0)?,
                site: r.get::<_, String>(1)?,
                model: r.get::<_, String>(2)?,
                hits: r.get::<_, i64>(3)?,
                created: r.get::<_, i64>(4)?,
                last_hit: r.get::<_, i64>(5)?,
                expires: r.get::<_, i64>(6)?,
                in_tok: r.get::<_, i64>(7)?,
                out_tok: r.get::<_, i64>(8)?,
                content_id: r.get::<_, String>(9)?,
            })
        })?
        .collect::<Result<_, _>>()?;

    match rows.len() {
        0 => anyhow::bail!("No cache entry found matching prefix '{}'", prefix),
        1 => {}
        _ => {
            let listed = rows
                .iter()
                .map(|r| format!("  {}", &r.key[..16]))
                .collect::<Vec<_>>()
                .join("\n");
            anyhow::bail!(
                "Ambiguous prefix '{}' — matches {} entries:\n{}",
                prefix,
                rows.len(),
                listed
            );
        }
    }

    let row = &rows[0];
    let (key, site, model, hits) = (&row.key, &row.site, &row.model, row.hits);
    let (created, last_hit, expires) = (row.created, row.last_hit, row.expires);
    let (in_tok, out_tok, content_id) = (row.in_tok, row.out_tok, &row.content_id);

    let output_preview: Option<String> = conn
        .query_row(
            "SELECT content_text FROM output_content WHERE content_id = ?1",
            rusqlite::params![content_id],
            |r| {
                let bytes: Vec<u8> = r.get(0)?;
                Ok(String::from_utf8_lossy(&bytes[..bytes.len().min(200)]).into_owned())
            },
        )
        .ok();

    println!("Cache entry: {}", &key[..key.len().min(24)]);
    println!("  Call site:       {site}");
    println!("  Model:           {model}");
    println!("  Hit count:       {hits}");
    println!("  Created:         {}", format_unix_micros(created));
    println!("  Last hit:        {}", format_unix_micros(last_hit));
    println!("  Expires:         {}", format_unix_micros(expires));
    println!("  Input tokens:    {}", format_count(in_tok as u64));
    println!("  Output tokens:   {}", format_count(out_tok as u64));
    println!("  Output content:  {}", &content_id[..content_id.len().min(16)]);
    if let Some(preview) = output_preview {
        println!("  Output (first 200 chars):");
        for line in preview.lines().take(4) {
            println!("    {line}");
        }
    }
    Ok(())
}

fn cmd_cache_evict(
    older_than: Option<String>,
    pattern: Option<String>,
    all: bool,
    storage_path: String,
) -> anyhow::Result<()> {
    let (conn, db_path) = open_canonical_for_cache(&storage_path)?;
    let size_before = db_size_bytes(&db_path).unwrap_or(0);

    let (removed, label) = match (older_than, pattern, all) {
        (Some(dur), None, false) => {
            let dur_us = parse_duration_micros(&dur)?;
            let cutoff = now_micros().saturating_sub(dur_us);
            let n = agentc_memo::ffi::invalidate(
                &conn,
                agentc_memo::key::InvalidationPattern::OlderThan { micros: cutoff },
            );
            (n, format!("older than {dur}"))
        }
        (None, Some(glob), false) => {
            let n = agentc_memo::ffi::invalidate(
                &conn,
                agentc_memo::key::InvalidationPattern::CallSiteGlob(glob.clone()),
            );
            (n, format!("matching \"{glob}\""))
        }
        (None, None, true) => {
            let n = agentc_memo::ffi::invalidate(&conn, agentc_memo::key::InvalidationPattern::All);
            (n, "(all entries)".to_string())
        }
        _ => anyhow::bail!("specify exactly one of --older-than, --pattern, --all"),
    };

    // Opportunistic VACUUM. `maintenance` runs ttl_sweep + lru_evict + vacuum,
    // but we already deleted above, so run a standalone VACUUM via maintenance
    // with a large cap (no LRU bite, no extra TTL kill since we just swept).
    let (_, _, _) = agentc_memo::ffi::maintenance(&conn, u64::MAX);

    let size_after = db_size_bytes(&db_path).unwrap_or(size_before);
    let reclaimed = size_before.saturating_sub(size_after);
    if reclaimed > 0 {
        println!(
            "Evicted {} entries {} ({} reclaimed).",
            format_count(removed),
            label,
            format_bytes_human(reclaimed)
        );
    } else {
        println!("Evicted {} entries {}.", format_count(removed), label);
    }
    Ok(())
}

fn cmd_cache_bench(
    call_site: String,
    runs: u64,
    _shadow: bool,
    _storage_path: String,
) -> anyhow::Result<()> {
    // The reference agent harness (M9) is not yet wired. Until then, `bench`
    // reports that its prerequisites are missing rather than pretending to run.
    anyhow::bail!(
        "agentc cache bench requires the reference-agent harness (tracked in bd-j3k / M9). \
         Requested: call_site={call_site}, runs={runs}."
    )
}

// =============================================================================
// agentc optimize ...
// =============================================================================

fn cmd_optimize(cmd: OptimizeCmd) -> anyhow::Result<()> {
    match cmd {
        OptimizeCmd::Report {
            hours,
            storage_path,
        } => cmd_optimize_report(hours, storage_path),
        OptimizeCmd::Inspect {
            call_site,
            storage_path,
        } => cmd_optimize_inspect(call_site, storage_path),
        OptimizeCmd::Disable {
            rule,
            call_site,
            reason,
            hours,
            storage_path,
        } => cmd_optimize_disable(rule, call_site, reason, hours, storage_path),
        OptimizeCmd::Bench {
            agent,
            storage_path,
        } => cmd_optimize_bench(agent, storage_path),
    }
}

/// Open (create if missing) `cost_model.db` in the storage dir. Returns the
/// connection; ensures the schema is applied.
fn open_cost_model_db(storage_dir: &std::path::Path) -> anyhow::Result<rusqlite::Connection> {
    std::fs::create_dir_all(storage_dir)?;
    let db_path = storage_dir.join("cost_model.db");
    let conn = rusqlite::Connection::open(&db_path)?;
    agentc_optimizer::schema::ensure_cost_model_schema(&conn)?;
    Ok(conn)
}

/// Open (create if missing) `optimizer_audit.db` in the storage dir. Returns
/// the connection; ensures the schema is applied.
fn open_audit_db(storage_dir: &std::path::Path) -> anyhow::Result<rusqlite::Connection> {
    std::fs::create_dir_all(storage_dir)?;
    let db_path = storage_dir.join("optimizer_audit.db");
    let conn = rusqlite::Connection::open(&db_path)?;
    agentc_optimizer::schema::ensure_audit_schema(&conn)?;
    Ok(conn)
}

fn now_us() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_micros() as i64)
        .unwrap_or(0)
}

fn cmd_optimize_report(hours: u64, storage_path: String) -> anyhow::Result<()> {
    let storage_dir = resolve_storage_path(&storage_path);
    let audit = open_audit_db(&storage_dir)?;
    let cost = open_cost_model_db(&storage_dir)?;
    let report = agentc_optimizer::build_report(&audit, Some(&cost), now_us(), hours)?;
    print!("{}", agentc_optimizer::render_report(&report));
    Ok(())
}

fn cmd_optimize_inspect(call_site: String, storage_path: String) -> anyhow::Result<()> {
    let storage_dir = resolve_storage_path(&storage_path);
    let cost = open_cost_model_db(&storage_dir)?;
    let audit = open_audit_db(&storage_dir)?;
    match agentc_optimizer::build_inspect(&cost, &audit, &call_site, 50, now_us())? {
        Some(inspect) => {
            print!("{}", agentc_optimizer::render_inspect(&inspect));
            Ok(())
        }
        None => {
            anyhow::bail!(
                "No cost-model profile for call site '{}'. Run the agent under \
                 `agentc record` so the optimizer observes it first.",
                call_site
            )
        }
    }
}

fn cmd_optimize_disable(
    rule: String,
    call_site: String,
    reason: String,
    hours: u64,
    storage_path: String,
) -> anyhow::Result<()> {
    let storage_dir = resolve_storage_path(&storage_path);
    let mut cost = open_cost_model_db(&storage_dir)?;
    let now = now_us();
    let cooldown_us = (hours as i64).saturating_mul(3_600 * 1_000_000);
    let reenable_at = now.saturating_add(cooldown_us);
    let summary = agentc_optimizer::disable_rule(
        &mut cost,
        &rule,
        &call_site,
        &reason,
        now,
        reenable_at,
    )?;
    print!("{}", agentc_optimizer::render_disable_summary(&summary));
    Ok(())
}

/// `agentc optimize bench --agent <path>`: run the agent twice and diff.
///
/// Pass 1: `AGENTC_OPTIMIZE=0` — profiling runs, optimizer is a no-op.
/// Pass 2: default — optimizer is active.
///
/// Each pass is captured under a fresh subdirectory of the storage path so
/// the two runs never share a cost model or audit DB. The actual diff is
/// then computed from their canonical traces.
fn cmd_optimize_bench(agent: String, storage_path: String) -> anyhow::Result<()> {
    let agent_path = std::path::PathBuf::from(&agent);
    if !agent_path.exists() {
        anyhow::bail!("Agent script not found: {}", agent);
    }

    let base_dir = resolve_storage_path(&storage_path);
    std::fs::create_dir_all(&base_dir)?;
    let baseline_dir = base_dir.join("bench-baseline");
    let optimized_dir = base_dir.join("bench-optimized");
    // Clean slate for each run — otherwise stale traces pollute the diff.
    let _ = std::fs::remove_dir_all(&baseline_dir);
    let _ = std::fs::remove_dir_all(&optimized_dir);
    std::fs::create_dir_all(&baseline_dir)?;
    std::fs::create_dir_all(&optimized_dir)?;

    eprintln!("Running baseline (optimizer disabled)...");
    let baseline = bench_run(&agent_path, &baseline_dir, false)?;
    eprintln!("Running optimized...");
    let optimized = bench_run(&agent_path, &optimized_dir, true)?;

    println!("─────────────────────────────────────────────────────────");
    println!(
        "Baseline:     {}   avg {:.1}s per task",
        format_usd(baseline.total_cost_usd),
        baseline.mean_duration_s
    );
    println!(
        "Optimized:    {}   avg {:.1}s per task",
        format_usd(optimized.total_cost_usd),
        optimized.mean_duration_s
    );
    let savings_frac = if baseline.total_cost_usd > 0.0 {
        (baseline.total_cost_usd - optimized.total_cost_usd) / baseline.total_cost_usd
    } else {
        0.0
    };
    let latency_delta = if baseline.mean_duration_s > 0.0 {
        (optimized.mean_duration_s - baseline.mean_duration_s) / baseline.mean_duration_s
    } else {
        0.0
    };
    println!(
        "Savings:      {:.1}%    (latency {:+.1}%)",
        savings_frac * 100.0,
        latency_delta * 100.0
    );
    Ok(())
}

struct BenchRun {
    total_cost_usd: f64,
    mean_duration_s: f64,
}

fn bench_run(
    agent: &std::path::Path,
    storage_dir: &std::path::Path,
    optimize: bool,
) -> anyhow::Result<BenchRun> {
    use std::process::Command;

    // Use `agentc record -- python <agent>` so profiling is wired the same
    // way it is for end users. The current process is the bench harness, not
    // the child — re-invoke the bin with Record.
    let self_exe = std::env::current_exe()
        .unwrap_or_else(|_| std::path::PathBuf::from("agentc"));
    let status = Command::new(&self_exe)
        .arg("record")
        .arg("--storage-path")
        .arg(storage_dir)
        .arg("--")
        .arg("python")
        .arg(agent)
        .env("AGENTC_OPTIMIZE", if optimize { "1" } else { "0" })
        .status()?;
    if !status.success() {
        anyhow::bail!("agent run failed (exit code {:?})", status.code());
    }

    // Aggregate cost + wall-clock duration from traces.db.
    let db_path = storage_dir.join("traces.db");
    if !db_path.exists() {
        return Ok(BenchRun {
            total_cost_usd: 0.0,
            mean_duration_s: 0.0,
        });
    }
    let conn = agentc_core::db::open_db(&db_path)?;
    agentc_analyzer::cost::full_cost_backfill(&conn)?;
    let (total_cost, duration_us, trace_count): (f64, i64, i64) = conn
        .query_row(
            "SELECT COALESCE(SUM(cost_usd), 0.0), \
                    COALESCE(MAX(COALESCE(end_time, start_time)) - MIN(start_time), 0), \
                    COUNT(DISTINCT trace_id) \
             FROM spans",
            [],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )?;
    let mean_duration_s = if trace_count > 0 {
        (duration_us as f64 / 1_000_000.0) / trace_count as f64
    } else {
        0.0
    };
    Ok(BenchRun {
        total_cost_usd: total_cost,
        mean_duration_s,
    })
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

    #[test]
    fn test_parse_duration_micros_units() {
        assert_eq!(parse_duration_micros("1s").unwrap(), 1_000_000);
        assert_eq!(parse_duration_micros("2m").unwrap(), 120_000_000);
        assert_eq!(parse_duration_micros("1h").unwrap(), 3_600_000_000);
        assert_eq!(parse_duration_micros("7d").unwrap(), 7 * 86_400 * 1_000_000);
    }

    #[test]
    fn test_parse_duration_micros_rejects_bad_input() {
        assert!(parse_duration_micros("").is_err());
        assert!(parse_duration_micros("10").is_err()); // no unit
        assert!(parse_duration_micros("10x").is_err()); // unknown unit
        assert!(parse_duration_micros("abc s").is_err());
    }

    #[test]
    fn test_format_count_inserts_separators() {
        assert_eq!(format_count(0), "0");
        assert_eq!(format_count(999), "999");
        assert_eq!(format_count(1_000), "1,000");
        assert_eq!(format_count(1_234_567), "1,234,567");
    }

    #[test]
    fn test_format_bytes_human_scales() {
        assert_eq!(format_bytes_human(512), "512 B");
        assert_eq!(format_bytes_human(2 * 1024), "2.0 KB");
        assert_eq!(format_bytes_human(5 * 1024 * 1024), "5.0 MB");
        assert_eq!(format_bytes_human(3 * 1024 * 1024 * 1024), "3.0 GB");
    }

    #[test]
    fn test_civil_from_epoch_seconds_known_dates() {
        // Epoch.
        assert_eq!(civil_from_epoch_seconds(0), (1970, 1, 1, 0, 0, 0));
        // 2000-01-01 00:00:00 UTC = 946684800.
        assert_eq!(
            civil_from_epoch_seconds(946_684_800),
            (2000, 1, 1, 0, 0, 0)
        );
        // 2020-02-29 12:00:00 UTC = 1582977600 (leap day).
        assert_eq!(
            civil_from_epoch_seconds(1_582_977_600),
            (2020, 2, 29, 12, 0, 0)
        );
    }

    #[test]
    fn test_percentile_empty_is_none() {
        let mut v: Vec<f64> = vec![];
        assert!(percentile(&mut v, 0.99).is_none());
    }

    #[test]
    fn test_percentile_basic() {
        let mut v: Vec<f64> = vec![1.0, 2.0, 3.0, 4.0, 5.0];
        assert_eq!(percentile(&mut v, 0.5), Some(3.0));
        let mut v2: Vec<f64> = vec![10.0];
        assert_eq!(percentile(&mut v2, 0.99), Some(10.0));
    }
}
