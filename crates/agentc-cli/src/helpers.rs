//! Shared helpers for CLI commands: storage path resolution, DB opening,
//! trace ID prefix matching, date parsing, attribute extraction.

use std::path::{Path, PathBuf};

use anyhow::{anyhow, Context, Result};
use rusqlite::{params, Connection};

/// Resolve a storage path, expanding `~` to the user's home directory.
pub fn resolve_storage_path(path: &str) -> PathBuf {
    if let Some(rest) = path.strip_prefix("~/") {
        dirs::home_dir()
            .unwrap_or_else(|| PathBuf::from("."))
            .join(rest)
    } else if path == "~" {
        dirs::home_dir().unwrap_or_else(|| PathBuf::from("."))
    } else {
        PathBuf::from(path)
    }
}

/// Open the canonical traces.db in `storage_dir` for read-only use.
///
/// Returns `Ok(None)` if the DB file does not exist — callers render "no data"
/// output in that case rather than erroring.
pub fn open_canonical_db_if_exists(storage_dir: &Path) -> Result<Option<Connection>> {
    agentc_core::hardening::audit_storage_dir(storage_dir)
        .with_context(|| format!("audit failed for storage dir {}", storage_dir.display()))?;

    let db_path = storage_dir.join("traces.db");
    if !db_path.exists() {
        return Ok(None);
    }

    let conn = agentc_core::db::open_db(&db_path)
        .with_context(|| format!("open traces.db at {}", db_path.display()))?;
    Ok(Some(conn))
}

/// Attempt to merge any pending per-process DBs into the canonical store.
///
/// Logs (but does not propagate) errors — merging is best-effort on the read path.
#[cfg(unix)]
pub fn try_merge_pending() {
    match agentc_core::merge::merge_all_pending() {
        Ok(stats) if stats.spans_merged > 0 => {
            eprintln!("Merged {} pending spans.", stats.spans_merged);
        }
        Ok(_) => {}
        Err(e) => {
            eprintln!("WARN: merge of pending DBs failed: {e}");
        }
    }
}

#[cfg(not(unix))]
pub fn try_merge_pending() {}

/// Resolve a trace ID prefix to a single full trace ID.
///
/// Requires prefix >= 4 chars. Returns an error if no match, or if multiple
/// traces match the prefix (listing the ambiguous IDs).
pub fn resolve_trace_id_prefix(conn: &Connection, prefix: &str) -> Result<String> {
    if prefix.len() < 4 {
        anyhow::bail!(
            "Trace ID prefix must be at least 4 characters, got: '{}'",
            prefix
        );
    }

    let like_pattern = format!("{prefix}%");
    let mut stmt = conn.prepare(
        "SELECT DISTINCT trace_id FROM spans WHERE trace_id LIKE ?1 ORDER BY trace_id LIMIT 10",
    )?;
    let matches: Vec<String> = stmt
        .query_map(params![like_pattern], |row| row.get(0))?
        .collect::<std::result::Result<Vec<_>, _>>()?;

    match matches.len() {
        0 => Err(anyhow!("No trace found matching prefix '{}'", prefix)),
        1 => Ok(matches.into_iter().next().unwrap()),
        _ => {
            let listed = matches
                .iter()
                .map(|s| format!("  {s}"))
                .collect::<Vec<_>>()
                .join("\n");
            Err(anyhow!(
                "Ambiguous trace prefix '{}' — matches {} traces:\n{}",
                prefix,
                matches.len(),
                listed,
            ))
        }
    }
}

/// Find the most recent trace's ID by MAX(start_time).
pub fn most_recent_trace_id(conn: &Connection) -> Result<Option<String>> {
    let result: Option<String> = conn
        .query_row(
            "SELECT trace_id FROM spans ORDER BY start_time DESC LIMIT 1",
            [],
            |row| row.get(0),
        )
        .ok();
    Ok(result)
}

/// Parse `YYYY-MM-DD` into Unix microseconds at 00:00:00 UTC.
pub fn parse_since_date(date_str: &str) -> Result<i64> {
    let parts: Vec<&str> = date_str.split('-').collect();
    if parts.len() != 3 {
        anyhow::bail!("Expected YYYY-MM-DD, got '{}'", date_str);
    }
    let year: i32 = parts[0]
        .parse()
        .with_context(|| format!("bad year in '{date_str}'"))?;
    let month: u32 = parts[1]
        .parse()
        .with_context(|| format!("bad month in '{date_str}'"))?;
    let day: u32 = parts[2]
        .parse()
        .with_context(|| format!("bad day in '{date_str}'"))?;

    if !(1..=12).contains(&month) || !(1..=31).contains(&day) {
        anyhow::bail!("Date out of range: '{}'", date_str);
    }

    let days = days_since_epoch(year, month, day);
    Ok(days * 86_400 * 1_000_000)
}

/// Days since Unix epoch for (year, month, day). Uses the civil_to_days
/// algorithm from Howard Hinnant's date library.
fn days_since_epoch(year: i32, month: u32, day: u32) -> i64 {
    let y = if month <= 2 { year - 1 } else { year } as i64;
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = (y - era * 400) as u64;
    let m = month as u64;
    let d = day as u64;
    let doy = (153 * (if m > 2 { m - 3 } else { m + 9 }) + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146097 + doe as i64 - 719468
}

/// Extract `gen_ai.agent.name` from a span's attributes JSON.
pub fn extract_agent_name(attributes_json: &str) -> Option<String> {
    let value: serde_json::Value = serde_json::from_str(attributes_json).ok()?;
    value
        .get("gen_ai.agent.name")
        .and_then(|v| v.as_str())
        .map(|s| s.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_resolve_storage_path_tilde() {
        let resolved = resolve_storage_path("~/.agentc");
        let home = dirs::home_dir().unwrap();
        assert_eq!(resolved, home.join(".agentc"));
    }

    #[test]
    fn test_resolve_storage_path_absolute() {
        assert_eq!(
            resolve_storage_path("/tmp/agentc"),
            PathBuf::from("/tmp/agentc")
        );
    }

    #[test]
    fn test_resolve_storage_path_tilde_only() {
        let home = dirs::home_dir().unwrap();
        assert_eq!(resolve_storage_path("~"), home);
    }

    #[test]
    fn test_parse_since_date() {
        // 2026-03-17 at 00:00:00 UTC.
        // days = 56*365 + 14 leap + 31 + 28 + 16 = 20529 → 20529*86400*1M = 1_773_705_600_000_000
        let us = parse_since_date("2026-03-17").unwrap();
        assert_eq!(us, 1_773_705_600_000_000);
    }

    #[test]
    fn test_parse_since_date_epoch() {
        assert_eq!(parse_since_date("1970-01-01").unwrap(), 0);
    }

    #[test]
    fn test_parse_since_date_invalid_format() {
        assert!(parse_since_date("03/17/2026").is_err());
        assert!(parse_since_date("2026").is_err());
        assert!(parse_since_date("not-a-date").is_err());
    }

    #[test]
    fn test_parse_since_date_out_of_range() {
        assert!(parse_since_date("2026-13-01").is_err());
        assert!(parse_since_date("2026-03-32").is_err());
    }

    #[test]
    fn test_extract_agent_name_present() {
        let attrs = r#"{"gen_ai.agent.name": "review-agent", "other": 1}"#;
        assert_eq!(
            extract_agent_name(attrs),
            Some("review-agent".to_string())
        );
    }

    #[test]
    fn test_extract_agent_name_absent() {
        assert_eq!(extract_agent_name("{}"), None);
        assert_eq!(extract_agent_name(r#"{"foo": 1}"#), None);
    }

    #[test]
    fn test_extract_agent_name_malformed() {
        assert_eq!(extract_agent_name("not json"), None);
        assert_eq!(extract_agent_name(""), None);
    }

    fn make_test_db_with_traces(trace_ids: &[&str]) -> (tempfile::TempDir, Connection) {
        let dir = tempfile::TempDir::new().unwrap();
        let path = dir.path().join("traces.db");
        let conn = agentc_core::db::create_db(&path, true).unwrap();

        for (i, tid) in trace_ids.iter().enumerate() {
            let span = agentc_core::span::Span {
                span_id: format!("span-{i}"),
                trace_id: tid.to_string(),
                parent_span_id: None,
                name: "test".to_string(),
                kind: "chat".to_string(),
                start_time: 1_000_000 + i as i64 * 1_000_000,
                end_time: Some(2_000_000 + i as i64 * 1_000_000),
                status: "OK".to_string(),
                model: Some("claude-sonnet-4".to_string()),
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
        }
        (dir, conn)
    }

    #[test]
    fn test_resolve_trace_id_prefix_unique_match() {
        let (_dir, conn) = make_test_db_with_traces(&[
            "abcd1234efgh",
            "wxyz9876mnop",
        ]);
        let resolved = resolve_trace_id_prefix(&conn, "abcd").unwrap();
        assert_eq!(resolved, "abcd1234efgh");
    }

    #[test]
    fn test_resolve_trace_id_prefix_too_short() {
        let (_dir, conn) = make_test_db_with_traces(&["abcd1234"]);
        let err = resolve_trace_id_prefix(&conn, "abc").unwrap_err();
        assert!(err.to_string().contains("at least 4"));
    }

    #[test]
    fn test_resolve_trace_id_prefix_no_match() {
        let (_dir, conn) = make_test_db_with_traces(&["abcd1234"]);
        let err = resolve_trace_id_prefix(&conn, "zzzz").unwrap_err();
        assert!(err.to_string().contains("No trace found"));
    }

    #[test]
    fn test_resolve_trace_id_prefix_ambiguous() {
        let (_dir, conn) = make_test_db_with_traces(&[
            "abcd1234",
            "abcd5678",
            "wxyz1234",
        ]);
        let err = resolve_trace_id_prefix(&conn, "abcd").unwrap_err();
        let msg = err.to_string();
        assert!(msg.contains("Ambiguous"));
        assert!(msg.contains("abcd1234"));
        assert!(msg.contains("abcd5678"));
    }

    #[test]
    fn test_most_recent_trace_id() {
        let (_dir, conn) = make_test_db_with_traces(&["aaaa1234", "bbbb5678"]);
        // bbbb5678 has the later start_time (index 1).
        let tid = most_recent_trace_id(&conn).unwrap();
        assert_eq!(tid, Some("bbbb5678".to_string()));
    }

    #[test]
    fn test_most_recent_trace_id_empty() {
        let dir = tempfile::TempDir::new().unwrap();
        let path = dir.path().join("traces.db");
        let conn = agentc_core::db::create_db(&path, true).unwrap();
        assert_eq!(most_recent_trace_id(&conn).unwrap(), None);
    }

    #[test]
    fn test_open_canonical_db_missing_file() {
        let dir = tempfile::TempDir::new().unwrap();
        // Fresh dir with no traces.db.
        let result = open_canonical_db_if_exists(dir.path()).unwrap();
        assert!(result.is_none());
    }

    #[test]
    fn test_open_canonical_db_existing_file() {
        let dir = tempfile::TempDir::new().unwrap();
        let path = dir.path().join("traces.db");
        let _ = agentc_core::db::create_db(&path, true).unwrap();
        // Drop the connection before reopening.
        let result = open_canonical_db_if_exists(dir.path()).unwrap();
        assert!(result.is_some());
    }
}
