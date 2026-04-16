//! Report rendering for the `agentc analyze`, `agentc traces`, and `agentc report` commands.
//!
//! This module is pure rendering: callers assemble display-ready structs (from DB queries,
//! cost backfill, and waste analysis) and pass them in. Output is plain text using
//! fixed-width columns, matching the formats defined in `specs/profiler.md`.

use std::collections::HashMap;

use crate::waste::{Confidence, WasteAnalysis};

// ===== Input structs =====

/// One row in the trace call breakdown table.
#[derive(Debug, Clone)]
pub struct CallRow {
    /// 1-based index of the call in the trace (by start_time).
    pub index: usize,
    pub agent: Option<String>,
    pub model: Option<String>,
    pub input_tokens: i64,
    pub output_tokens: i64,
    pub cost_usd: Option<f64>,
    /// Labels to render in the FLAGS column (e.g., `["context_bloat", "redundant_call (~#2)"]`).
    pub flag_labels: Vec<String>,
}

/// Full per-trace analysis output.
#[derive(Debug, Clone)]
pub struct TraceReport {
    pub trace_id: String,
    /// Root-span agent name; falls back to `trace_id` prefix when absent.
    pub agent_name: Option<String>,
    pub duration_us: i64,
    pub span_count: usize,
    pub total_input_tokens: i64,
    pub total_output_tokens: i64,
    pub total_cost_usd: f64,
    pub calls: Vec<CallRow>,
    pub waste: WasteAnalysis,
    /// Maps `span_id` → 1-based call index for the waste-report section.
    pub span_id_to_call_idx: HashMap<String, usize>,
}

/// One row in the `agentc traces` list.
#[derive(Debug, Clone)]
pub struct TracesListRow {
    pub trace_id: String,
    pub started_us: i64,
    pub duration_us: i64,
    pub span_count: usize,
    pub total_tokens: i64,
    pub total_cost_usd: f64,
    /// Detector name → flag count, in the order they should display.
    pub flag_counts: Vec<(String, usize)>,
}

/// Data for the `agentc traces` list.
#[derive(Debug, Clone, Default)]
pub struct TracesList {
    pub rows: Vec<TracesListRow>,
}

#[derive(Debug, Clone)]
pub struct ModelBreakdown {
    pub model: String,
    pub total_tokens: i64,
    pub total_cost_usd: f64,
}

#[derive(Debug, Clone)]
pub struct AgentBreakdown {
    pub agent: String,
    pub total_tokens: i64,
    pub total_cost_usd: f64,
}

#[derive(Debug, Clone)]
pub struct WasteDetectorSummary {
    pub detector: String,
    pub flag_count: usize,
    pub estimated_waste_usd: f64,
}

/// Data for `agentc report`.
#[derive(Debug, Clone, Default)]
pub struct AggregateReport {
    pub trace_count: usize,
    /// Earliest/latest trace start_time (Unix microseconds).
    pub date_start_us: Option<i64>,
    pub date_end_us: Option<i64>,
    pub total_tokens: i64,
    pub total_cost_usd: f64,
    pub by_model: Vec<ModelBreakdown>,
    pub by_agent: Vec<AgentBreakdown>,
    pub waste_summary: Vec<WasteDetectorSummary>,
}

// ===== Formatting helpers =====

/// Format a dollar amount with two decimal places, e.g., `$0.89`.
fn format_usd(v: f64) -> String {
    format!("${v:.2}")
}

/// Format a tilde-approximate dollar amount, e.g., `~$0.62`.
fn format_usd_approx(v: f64) -> String {
    format!("~${v:.2}")
}

/// Format an integer with comma thousands separators, e.g., `252,031`.
fn format_count(n: i64) -> String {
    let negative = n < 0;
    let mut digits: Vec<u8> = n.unsigned_abs().to_string().into_bytes();
    let mut out: Vec<u8> = Vec::with_capacity(digits.len() + digits.len() / 3);
    while digits.len() > 3 {
        let idx = digits.len() - 3;
        let chunk = digits.split_off(idx);
        out.splice(0..0, std::iter::once(b',').chain(chunk));
    }
    out.splice(0..0, digits);
    let mut s = String::from_utf8(out).unwrap();
    if negative {
        s.insert(0, '-');
    }
    s
}

/// Format a microsecond duration as `47.2s` or `1m 3.8s`.
fn format_duration_us(us: i64) -> String {
    let secs = us as f64 / 1_000_000.0;
    if secs < 60.0 {
        format!("{secs:.1}s")
    } else {
        let mins = (secs / 60.0).floor() as i64;
        let rem = secs - (mins as f64) * 60.0;
        format!("{mins}m {rem:.1}s")
    }
}

/// Format a Unix microsecond timestamp as `YYYY-MM-DD HH:MM:SS` (UTC).
fn format_timestamp_us(us: i64) -> String {
    if us < 0 {
        return "-".to_string();
    }
    let secs = us / 1_000_000;
    let sod = (secs % 86400) as u32;
    let days = secs / 86400;
    let (hour, min, sec) = (sod / 3600, (sod / 60) % 60, sod % 60);
    let (y, mo, d) = date_from_epoch_days(days);
    format!("{y:04}-{mo:02}-{d:02} {hour:02}:{min:02}:{sec:02}")
}

/// Convert days since Unix epoch (1970-01-01) to (year, month, day). Uses the
/// civil_from_days algorithm from Howard Hinnant's date library.
fn date_from_epoch_days(days: i64) -> (i32, u32, u32) {
    let z = days + 719468;
    let era = if z >= 0 { z } else { z - 146096 } / 146097;
    let doe = (z - era * 146097) as u64;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let y = yoe as i64 + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = if m <= 2 { y + 1 } else { y };
    (y as i32, m as u32, d as u32)
}

fn confidence_label(c: &Confidence) -> &'static str {
    match c {
        Confidence::High => "high",
        Confidence::InputOnly => "input-only",
        Confidence::Low => "low",
    }
}

/// Per-detector recommendation text. Matches the wording in `specs/profiler.md`.
fn detector_advice(detector: &str) -> &'static str {
    match detector {
        "context_bloat" => {
            "Consider: truncate context to relevant sections, or split into focused sub-queries."
        }
        "redundant_call" => "Consider: cache the first result and reuse, or deduplicate upstream.",
        "retry_storm" => "Consider: add backoff with jitter, or surface the underlying error instead of retrying blindly.",
        "model_overkill" => "Consider: use a cheaper model (e.g., haiku-class) for short or simple tasks.",
        "cache_miss_repeat" => "Consider: enable prompt caching, or reorder calls so cached prefixes are reused.",
        _ => "Consider: review the flagged calls.",
    }
}

/// Short summary sentence for a waste flag, referring to call indices.
fn flag_summary_sentence(detector: &str, indices: &[usize]) -> String {
    let call_list = format_call_list(indices);
    match detector {
        "context_bloat" => format!(
            "Call{} {} sent a large fraction of the context window but received few output tokens.",
            plural_s(indices.len()),
            call_list,
        ),
        "redundant_call" => format!(
            "Call{} {} have high input similarity and may be producing duplicate work.",
            plural_s(indices.len()),
            call_list,
        ),
        "retry_storm" => format!(
            "Call{} {} fired rapidly against the same model with near-identical input.",
            plural_s(indices.len()),
            call_list,
        ),
        "model_overkill" => format!(
            "Call{} {} used a frontier model for a small task.",
            plural_s(indices.len()),
            call_list,
        ),
        "cache_miss_repeat" => format!(
            "Call{} {} sent the same input as an earlier call without hitting the cache.",
            plural_s(indices.len()),
            call_list,
        ),
        _ => format!(
            "Call{} {} were flagged by detector `{}`.",
            plural_s(indices.len()),
            call_list,
            detector,
        ),
    }
}

fn plural_s(n: usize) -> &'static str {
    if n == 1 {
        ""
    } else {
        "s"
    }
}

/// Render a list of 1-based call indices as `#2, #3, #5`.
fn format_call_list(indices: &[usize]) -> String {
    let mut sorted = indices.to_vec();
    sorted.sort_unstable();
    sorted
        .into_iter()
        .map(|i| format!("#{i}"))
        .collect::<Vec<_>>()
        .join(", ")
}

/// Truncate a string to at most `width` chars, appending `…` when truncated.
fn truncate_str(s: &str, width: usize) -> String {
    if s.chars().count() <= width {
        return s.to_string();
    }
    if width == 0 {
        return String::new();
    }
    let mut out: String = s.chars().take(width - 1).collect();
    out.push('…');
    out
}

// ===== Render: trace analysis =====

/// Render the full `agentc analyze` output for a single trace.
pub fn render_trace_analysis(report: &TraceReport) -> String {
    let mut out = String::new();

    // --- Header ---
    let name = report
        .agent_name
        .as_deref()
        .unwrap_or_else(|| trace_id_prefix(&report.trace_id));
    let total_tokens = report.total_input_tokens + report.total_output_tokens;
    out.push_str(&format!(
        " Trace: {} | {} | {} spans | {} tokens | {}\n",
        name,
        format_duration_us(report.duration_us),
        report.span_count,
        format_count(total_tokens),
        format_usd(report.total_cost_usd),
    ));
    out.push('\n');

    // --- Call breakdown ---
    out.push_str(" CALL BREAKDOWN\n");
    out.push_str(&format!(
        " {:>3} {:<16} {:<24} {:>8} {:>7} {:>7}  FLAGS\n",
        "#", "AGENT", "MODEL", "IN", "OUT", "COST",
    ));

    if report.calls.is_empty() {
        out.push_str(" (no LLM calls recorded)\n");
    } else {
        for call in &report.calls {
            let agent = call.agent.as_deref().unwrap_or("-");
            let model = call.model.as_deref().unwrap_or("-");
            let cost = call
                .cost_usd
                .map(format_usd)
                .unwrap_or_else(|| "-".to_string());
            let flags = if call.flag_labels.is_empty() {
                "--".to_string()
            } else {
                call.flag_labels.join(", ")
            };
            out.push_str(&format!(
                " {:>3} {:<16} {:<24} {:>8} {:>7} {:>7}  {}\n",
                call.index,
                truncate_str(agent, 16),
                truncate_str(model, 24),
                format_count(call.input_tokens),
                format_count(call.output_tokens),
                cost,
                flags,
            ));
        }
    }

    out.push('\n');

    // --- Waste report ---
    out.push_str(" WASTE REPORT\n");
    if report.waste.flags.is_empty() {
        out.push_str(" (no waste flags detected)\n");
        return out;
    }

    // Group flags by detector in the order they appear.
    let mut detector_order: Vec<String> = Vec::new();
    let mut grouped: HashMap<String, Vec<&crate::waste::WasteFlag>> = HashMap::new();
    for flag in &report.waste.flags {
        if !grouped.contains_key(&flag.detector) {
            detector_order.push(flag.detector.clone());
        }
        grouped.entry(flag.detector.clone()).or_default().push(flag);
    }

    for detector in &detector_order {
        let flags = grouped.get(detector).unwrap();
        let total_cost: f64 = flags.iter().filter_map(|f| f.estimated_cost).sum();
        let call_count: usize = flags.iter().map(|f| f.span_ids.len()).sum();

        let cost_str = if total_cost > 0.0 {
            format!(", {} wasted", format_usd_approx(total_cost))
        } else {
            String::new()
        };
        out.push_str(&format!(
            " {} ({} call{}{})\n",
            detector,
            call_count,
            plural_s(call_count),
            cost_str,
        ));

        for flag in flags {
            let indices: Vec<usize> = flag
                .span_ids
                .iter()
                .filter_map(|sid| report.span_id_to_call_idx.get(sid).copied())
                .collect();
            out.push_str(&format!(
                "   {} (confidence: {})\n",
                flag_summary_sentence(detector, &indices),
                confidence_label(&flag.confidence),
            ));
        }
        out.push_str(&format!("   -> {}\n", detector_advice(detector)));
    }

    out.push('\n');
    out.push_str(&format!(
        " Total flagged waste: {} of {} spend (deduplicated — per-span MAX, not sum)\n",
        format_usd_approx(report.waste.total_waste_usd),
        format_usd(report.total_cost_usd),
    ));

    out
}

/// Return the first 8 chars of a trace ID, or the whole thing if shorter.
fn trace_id_prefix(trace_id: &str) -> &str {
    let end = trace_id
        .char_indices()
        .nth(8)
        .map(|(i, _)| i)
        .unwrap_or(trace_id.len());
    &trace_id[..end]
}

// ===== Render: traces list =====

/// Render the `agentc traces` list with footer totals.
pub fn render_traces_list(list: &TracesList) -> String {
    let mut out = String::new();

    out.push_str(&format!(
        " {:<14} {:<22} {:>10} {:>6} {:>12} {:>10}  WASTE FLAGS\n",
        "TRACE ID", "STARTED", "DURATION", "SPANS", "TOKENS", "COST",
    ));

    if list.rows.is_empty() {
        out.push_str(" (no traces found)\n\n");
        out.push_str("0 traces | 0 total tokens | $0.00 total cost | 0 waste flags\n");
        return out;
    }

    let mut total_tokens: i64 = 0;
    let mut total_cost: f64 = 0.0;
    let mut total_flags: usize = 0;

    for row in &list.rows {
        let trace_display = format!("{}...", trace_id_prefix(&row.trace_id));
        let flags_str = if row.flag_counts.is_empty() {
            "--".to_string()
        } else {
            row.flag_counts
                .iter()
                .map(|(det, n)| format!("{det} ({n})"))
                .collect::<Vec<_>>()
                .join(", ")
        };

        out.push_str(&format!(
            " {:<14} {:<22} {:>10} {:>6} {:>12} {:>10}  {}\n",
            trace_display,
            format_timestamp_us(row.started_us),
            format_duration_us(row.duration_us),
            row.span_count,
            format_count(row.total_tokens),
            format_usd(row.total_cost_usd),
            flags_str,
        ));

        total_tokens += row.total_tokens;
        total_cost += row.total_cost_usd;
        total_flags += row.flag_counts.iter().map(|(_, n)| n).sum::<usize>();
    }

    out.push('\n');
    out.push_str(&format!(
        "{} trace{} | {} total tokens | {} total cost | {} waste flag{}\n",
        list.rows.len(),
        plural_s(list.rows.len()),
        format_count(total_tokens),
        format_usd(total_cost),
        total_flags,
        plural_s(total_flags),
    ));

    out
}

// ===== Render: aggregate report =====

/// Render the `agentc report` aggregate output.
pub fn render_aggregate_report(report: &AggregateReport) -> String {
    let mut out = String::new();

    // --- Summary header ---
    let date_range = match (report.date_start_us, report.date_end_us) {
        (Some(start), Some(end)) => format!(
            ", {} to {}",
            date_only(start),
            date_only(end),
        ),
        _ => String::new(),
    };
    out.push_str(&format!(
        " SUMMARY ({} trace{}{})\n",
        report.trace_count,
        plural_s(report.trace_count),
        date_range,
    ));

    let avg_cost = if report.trace_count > 0 {
        report.total_cost_usd / report.trace_count as f64
    } else {
        0.0
    };
    out.push_str(&format!(
        " Total tokens: {} | Total cost: {} | Avg cost/trace: {}\n",
        format_count(report.total_tokens),
        format_usd(report.total_cost_usd),
        format_usd(avg_cost),
    ));
    out.push('\n');

    // --- BY MODEL ---
    out.push_str(" BY MODEL\n");
    if report.by_model.is_empty() {
        out.push_str(" (no data)\n");
    } else {
        for row in &report.by_model {
            let pct = if report.total_cost_usd > 0.0 {
                row.total_cost_usd / report.total_cost_usd * 100.0
            } else {
                0.0
            };
            out.push_str(&format!(
                " {:<24} {:>14} tokens  {:>8}  ({:>5.1}%)\n",
                truncate_str(&row.model, 24),
                format_count(row.total_tokens),
                format_usd(row.total_cost_usd),
                pct,
            ));
        }
    }
    out.push('\n');

    // --- BY AGENT ---
    out.push_str(" BY AGENT\n");
    if report.by_agent.is_empty() {
        out.push_str(" (no data)\n");
    } else {
        for row in &report.by_agent {
            let pct = if report.total_cost_usd > 0.0 {
                row.total_cost_usd / report.total_cost_usd * 100.0
            } else {
                0.0
            };
            out.push_str(&format!(
                " {:<24} {:>14} tokens  {:>8}  ({:>5.1}%)\n",
                truncate_str(&row.agent, 24),
                format_count(row.total_tokens),
                format_usd(row.total_cost_usd),
                pct,
            ));
        }
    }
    out.push('\n');

    // --- WASTE SUMMARY ---
    out.push_str(" WASTE SUMMARY\n");
    if report.waste_summary.is_empty() {
        out.push_str(" (no waste flags detected)\n");
        return out;
    }

    let total_flags: usize = report.waste_summary.iter().map(|w| w.flag_count).sum();
    let total_waste: f64 = report
        .waste_summary
        .iter()
        .map(|w| w.estimated_waste_usd)
        .sum();

    for row in &report.waste_summary {
        out.push_str(&format!(
            " {:<20} {:>4} flag{}  {:>8} estimated waste\n",
            row.detector,
            row.flag_count,
            plural_s(row.flag_count),
            format_usd_approx(row.estimated_waste_usd),
        ));
    }
    let pct = if report.total_cost_usd > 0.0 {
        total_waste / report.total_cost_usd * 100.0
    } else {
        0.0
    };
    out.push_str(&format!("{}\n", "-".repeat(60)));
    out.push_str(&format!(
        " {:<20} {:>4} flag{}  {:>8} estimated waste ({:.1}% of total spend)\n",
        "TOTAL",
        total_flags,
        plural_s(total_flags),
        format_usd_approx(total_waste),
        pct,
    ));

    out
}

/// Format a Unix micro timestamp as `YYYY-MM-DD` (date only).
fn date_only(us: i64) -> String {
    if us < 0 {
        return "-".to_string();
    }
    let days = us / 1_000_000 / 86400;
    let (y, mo, d) = date_from_epoch_days(days);
    format!("{y:04}-{mo:02}-{d:02}")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::waste::{Confidence, WasteAnalysis, WasteFlag};

    // --- formatting helpers ---

    #[test]
    fn test_format_count_thousands() {
        assert_eq!(format_count(0), "0");
        assert_eq!(format_count(123), "123");
        assert_eq!(format_count(1_234), "1,234");
        assert_eq!(format_count(252_031), "252,031");
        assert_eq!(format_count(1_234_567_890), "1,234,567,890");
        assert_eq!(format_count(-1_234), "-1,234");
    }

    #[test]
    fn test_format_usd() {
        assert_eq!(format_usd(0.0), "$0.00");
        assert_eq!(format_usd(0.89), "$0.89");
        assert_eq!(format_usd(48.23), "$48.23");
    }

    #[test]
    fn test_format_duration_us() {
        assert_eq!(format_duration_us(0), "0.0s");
        assert_eq!(format_duration_us(47_200_000), "47.2s");
        assert_eq!(format_duration_us(63_800_000), "1m 3.8s");
        assert_eq!(format_duration_us(120_000_000), "2m 0.0s");
    }

    #[test]
    fn test_format_timestamp_us() {
        // 2026-03-17 14:23:01 UTC
        //   days since epoch = 56*365 + 14 leap days + 31 + 28 + 16 = 20529
        //   seconds = 20529 * 86400 + 14*3600 + 23*60 + 1 = 1_773_757_381
        let ts = 1_773_757_381_i64 * 1_000_000;
        assert_eq!(format_timestamp_us(ts), "2026-03-17 14:23:01");
    }

    #[test]
    fn test_date_from_epoch_days() {
        assert_eq!(date_from_epoch_days(0), (1970, 1, 1));
        assert_eq!(date_from_epoch_days(31), (1970, 2, 1));
        // 2026-03-17
        let d = (2026 - 1970) * 365 + 14 /* leap days */ + 31 /* Jan */ + 28 /* Feb */ + 17 - 1;
        assert_eq!(date_from_epoch_days(d), (2026, 3, 17));
    }

    #[test]
    fn test_format_call_list_sorts() {
        assert_eq!(format_call_list(&[3, 1, 2]), "#1, #2, #3");
        assert_eq!(format_call_list(&[5]), "#5");
    }

    #[test]
    fn test_truncate_str() {
        assert_eq!(truncate_str("abc", 10), "abc");
        assert_eq!(truncate_str("abcdefghij", 10), "abcdefghij");
        assert_eq!(truncate_str("abcdefghijk", 10), "abcdefghi…");
    }

    // --- render_trace_analysis ---

    fn sample_trace_report() -> TraceReport {
        let calls = vec![
            CallRow {
                index: 1,
                agent: Some("orchestrator".to_string()),
                model: Some("claude-sonnet-4".to_string()),
                input_tokens: 12_301,
                output_tokens: 1_204,
                cost_usd: Some(0.05),
                flag_labels: vec![],
            },
            CallRow {
                index: 2,
                agent: Some("review-agent".to_string()),
                model: Some("claude-sonnet-4".to_string()),
                input_tokens: 102_400,
                output_tokens: 87,
                cost_usd: Some(0.31),
                flag_labels: vec!["context_bloat".to_string()],
            },
        ];

        let waste = WasteAnalysis {
            flags: vec![WasteFlag {
                detector: "context_bloat".to_string(),
                span_ids: vec!["s2".to_string()],
                estimated_cost: Some(0.31),
                confidence: Confidence::High,
                description: "90% full".to_string(),
            }],
            total_waste_usd: 0.31,
        };

        let mut map = HashMap::new();
        map.insert("s1".to_string(), 1);
        map.insert("s2".to_string(), 2);

        TraceReport {
            trace_id: "a3f8c012abc".to_string(),
            agent_name: Some("code-review-agent".to_string()),
            duration_us: 47_200_000,
            span_count: 2,
            total_input_tokens: 114_701,
            total_output_tokens: 1_291,
            total_cost_usd: 0.36,
            calls,
            waste,
            span_id_to_call_idx: map,
        }
    }

    #[test]
    fn test_render_trace_analysis_happy_path() {
        let report = sample_trace_report();
        let out = render_trace_analysis(&report);

        assert!(out.contains("Trace: code-review-agent"));
        assert!(out.contains("47.2s"));
        assert!(out.contains("115,992 tokens"));
        assert!(out.contains("$0.36"));
        assert!(out.contains("CALL BREAKDOWN"));
        assert!(out.contains("orchestrator"));
        assert!(out.contains("review-agent"));
        assert!(out.contains("context_bloat"));
        assert!(out.contains("WASTE REPORT"));
        assert!(out.contains("~$0.31"));
        assert!(out.contains("-> Consider: truncate context"));
        assert!(out.contains("Total flagged waste"));
    }

    #[test]
    fn test_render_trace_analysis_no_waste() {
        let mut report = sample_trace_report();
        report.waste = WasteAnalysis::default();
        report.calls[1].flag_labels.clear();

        let out = render_trace_analysis(&report);
        assert!(out.contains("WASTE REPORT"));
        assert!(out.contains("(no waste flags detected)"));
        assert!(!out.contains("-> Consider:"));
    }

    #[test]
    fn test_render_trace_analysis_no_calls() {
        let mut report = sample_trace_report();
        report.calls.clear();
        report.waste = WasteAnalysis::default();

        let out = render_trace_analysis(&report);
        assert!(out.contains("(no LLM calls recorded)"));
    }

    #[test]
    fn test_render_trace_analysis_missing_agent_uses_trace_id() {
        let mut report = sample_trace_report();
        report.agent_name = None;

        let out = render_trace_analysis(&report);
        assert!(out.contains("Trace: a3f8c012"));
    }

    #[test]
    fn test_render_trace_analysis_waste_references_call_indices() {
        let report = sample_trace_report();
        let out = render_trace_analysis(&report);
        assert!(out.contains("#2"));
    }

    // --- render_traces_list ---

    #[test]
    fn test_render_traces_list_empty() {
        let list = TracesList::default();
        let out = render_traces_list(&list);
        assert!(out.contains("TRACE ID"));
        assert!(out.contains("(no traces found)"));
        assert!(out.contains("0 traces"));
        assert!(out.contains("0 waste flags"));
    }

    #[test]
    fn test_render_traces_list_with_rows() {
        let list = TracesList {
            rows: vec![
                TracesListRow {
                    trace_id: "a3f8c012abcdef".to_string(),
                    started_us: 1_773_217_381_000_000,
                    duration_us: 47_200_000,
                    span_count: 23,
                    total_tokens: 252_031,
                    total_cost_usd: 0.89,
                    flag_counts: vec![
                        ("context_bloat".to_string(), 2),
                        ("model_overkill".to_string(), 1),
                    ],
                },
                TracesListRow {
                    trace_id: "b7e1d045xxyyzz".to_string(),
                    started_us: 1_773_217_000_000_000,
                    duration_us: 12_100_000,
                    span_count: 8,
                    total_tokens: 31_450,
                    total_cost_usd: 0.19,
                    flag_counts: vec![],
                },
            ],
        };

        let out = render_traces_list(&list);
        assert!(out.contains("a3f8c012..."));
        assert!(out.contains("b7e1d045..."));
        assert!(out.contains("context_bloat (2)"));
        assert!(out.contains("--"));
        assert!(out.contains("2 traces"));
        assert!(out.contains("283,481 total tokens"));
        assert!(out.contains("$1.08"));
        assert!(out.contains("3 waste flags"));
    }

    // --- render_aggregate_report ---

    #[test]
    fn test_render_aggregate_report_empty() {
        let report = AggregateReport::default();
        let out = render_aggregate_report(&report);
        assert!(out.contains("SUMMARY (0 traces)"));
        assert!(out.contains("Total tokens: 0"));
        assert!(out.contains("BY MODEL"));
        assert!(out.contains("BY AGENT"));
        assert!(out.contains("(no data)"));
        assert!(out.contains("(no waste flags detected)"));
    }

    #[test]
    fn test_render_aggregate_report_happy_path() {
        let report = AggregateReport {
            trace_count: 50,
            date_start_us: Some(1_741_521_600_000_000),
            date_end_us: Some(1_742_126_400_000_000),
            total_tokens: 8_412_301,
            total_cost_usd: 48.23,
            by_model: vec![
                ModelBreakdown {
                    model: "claude-sonnet-4".to_string(),
                    total_tokens: 6_102_400,
                    total_cost_usd: 32.12,
                },
                ModelBreakdown {
                    model: "claude-opus-4".to_string(),
                    total_tokens: 1_890_301,
                    total_cost_usd: 14.89,
                },
            ],
            by_agent: vec![AgentBreakdown {
                agent: "review-agent".to_string(),
                total_tokens: 4_201_000,
                total_cost_usd: 24.11,
            }],
            waste_summary: vec![
                WasteDetectorSummary {
                    detector: "context_bloat".to_string(),
                    flag_count: 34,
                    estimated_waste_usd: 12.40,
                },
                WasteDetectorSummary {
                    detector: "redundant_call".to_string(),
                    flag_count: 18,
                    estimated_waste_usd: 8.90,
                },
            ],
        };

        let out = render_aggregate_report(&report);
        assert!(out.contains("SUMMARY (50 traces"));
        assert!(out.contains("Total tokens: 8,412,301"));
        assert!(out.contains("Total cost: $48.23"));
        assert!(out.contains("Avg cost/trace: $0.96"));
        assert!(out.contains("claude-sonnet-4"));
        assert!(out.contains("66.6%"));
        assert!(out.contains("review-agent"));
        assert!(out.contains("context_bloat"));
        assert!(out.contains("34 flags"));
        assert!(out.contains("~$12.40"));
        assert!(out.contains("TOTAL"));
        assert!(out.contains("52 flags"));
    }

    #[test]
    fn test_render_aggregate_report_date_range_formatting() {
        let report = AggregateReport {
            trace_count: 3,
            date_start_us: Some(1_741_521_600_000_000), // 2025-03-09
            date_end_us: Some(1_742_126_400_000_000),   // 2025-03-16
            ..Default::default()
        };
        let out = render_aggregate_report(&report);
        assert!(
            out.contains("2025-03-09") && out.contains("2025-03-16"),
            "expected date range, got: {out}"
        );
    }

    #[test]
    fn test_detector_advice_covers_all_detectors() {
        for detector in [
            "context_bloat",
            "redundant_call",
            "retry_storm",
            "model_overkill",
            "cache_miss_repeat",
        ] {
            let advice = detector_advice(detector);
            assert!(advice.starts_with("Consider:"), "{detector}: {advice}");
        }
    }
}
