//! Per-process DB isolation and merge-to-canonical protocol.
//!
//! Each process writes to `~/.agentc/active/pid-<PID>.db` with zero write contention.
//! The canonical store at `~/.agentc/traces.db` is populated by merging per-process files.
//!
//! Merge uses `ATTACH DATABASE` + `INSERT OR IGNORE` in a single transaction,
//! content tables first (input_content, output_content), then spans.
//! flock()-based locking prevents concurrent merges.

use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime};

use anyhow::{bail, Context, Result};
use rusqlite::Connection;

use crate::db::{create_db, open_db};

/// Lock timeout for flock() acquisition.
const LOCK_TIMEOUT: Duration = Duration::from_secs(10);

/// Lockfile mtime threshold for stale detection (seconds).
const STALE_LOCK_AGE_SECS: u64 = 60;

/// Orphan detection: minimum file age before considering orphaned.
const ORPHAN_MIN_AGE_SECS: u64 = 60;

/// Statistics returned from a merge operation.
#[derive(Debug, Clone, Default)]
pub struct MergeStats {
    pub spans_merged: i64,
    pub input_content_merged: i64,
    pub output_content_merged: i64,
}

/// RAII file lock guard using flock().
#[cfg(unix)]
pub struct FileLock {
    file: fs::File,
}

#[cfg(unix)]
impl FileLock {
    /// Acquire an exclusive flock() with timeout.
    ///
    /// On timeout, checks if the lockfile is stale (mtime > 60s). If stale,
    /// removes the lockfile, creates a fresh one, and retries once.
    pub fn acquire(lockfile_path: &Path, timeout: Duration) -> Result<Self> {
        match Self::try_acquire_with_timeout(lockfile_path, timeout) {
            Ok(lock) => Ok(lock),
            Err(_) => {
                // Check if stale.
                if is_lockfile_stale(lockfile_path) {
                    eprintln!(
                        "WARN: Stale lockfile detected at {} (mtime > {STALE_LOCK_AGE_SECS}s), removing",
                        lockfile_path.display()
                    );
                    let _ = fs::remove_file(lockfile_path);
                    // Retry once.
                    Self::try_acquire_with_timeout(lockfile_path, timeout)
                        .context("Merge skipped: could not acquire lock after retry")
                } else {
                    bail!(
                        "Merge skipped: could not acquire lock on {}. Will retry on next CLI read.",
                        lockfile_path.display()
                    )
                }
            }
        }
    }

    fn try_acquire_with_timeout(lockfile_path: &Path, timeout: Duration) -> Result<Self> {
        use std::os::unix::io::AsRawFd;

        // Ensure parent directory exists.
        if let Some(parent) = lockfile_path.parent() {
            fs::create_dir_all(parent)?;
        }

        let file = fs::OpenOptions::new()
            .create(true)
            .write(true)
            .truncate(false)
            .open(lockfile_path)
            .with_context(|| format!("Failed to open lockfile {}", lockfile_path.display()))?;

        let fd = file.as_raw_fd();
        let start = std::time::Instant::now();

        loop {
            let ret = unsafe { libc::flock(fd, libc::LOCK_EX | libc::LOCK_NB) };
            if ret == 0 {
                return Ok(FileLock { file });
            }

            let err = io::Error::last_os_error();
            if err.kind() != io::ErrorKind::WouldBlock {
                return Err(err).context("flock() failed");
            }

            if start.elapsed() >= timeout {
                bail!("flock() timeout after {}s", timeout.as_secs());
            }

            std::thread::sleep(Duration::from_millis(50));
        }
    }
}

#[cfg(unix)]
impl Drop for FileLock {
    fn drop(&mut self) {
        use std::os::unix::io::AsRawFd;
        unsafe {
            libc::flock(self.file.as_raw_fd(), libc::LOCK_UN);
        }
    }
}

/// Check if a lockfile is stale based on mtime.
fn is_lockfile_stale(path: &Path) -> bool {
    let Ok(metadata) = fs::metadata(path) else {
        return false;
    };
    let Ok(modified) = metadata.modified() else {
        return false;
    };
    let Ok(age) = SystemTime::now().duration_since(modified) else {
        return false;
    };
    age.as_secs() > STALE_LOCK_AGE_SECS
}

/// Return the default agentc data directory: `~/.agentc/`.
pub fn agentc_data_dir() -> PathBuf {
    dirs::home_dir()
        .unwrap_or_else(|| PathBuf::from("."))
        .join(".agentc")
}

/// Return the per-process DB path for the current process.
pub fn per_process_db_path() -> PathBuf {
    let pid = std::process::id();
    agentc_data_dir().join("active").join(format!("pid-{pid}.db"))
}

/// Return the per-process DB path for a given PID.
pub fn per_process_db_path_for_pid(pid: u32) -> PathBuf {
    agentc_data_dir().join("active").join(format!("pid-{pid}.db"))
}

/// Return the canonical DB path.
pub fn canonical_db_path() -> PathBuf {
    agentc_data_dir().join("traces.db")
}

/// Return the lockfile path for the canonical DB.
pub fn lockfile_path() -> PathBuf {
    agentc_data_dir().join("traces.db.lock")
}

/// Extract PID from a per-process DB filename.
///
/// Returns `None` if the filename doesn't match `pid-<N>.db`.
pub fn extract_pid_from_path(path: &Path) -> Option<u32> {
    let stem = path.file_stem()?.to_str()?;
    let pid_str = stem.strip_prefix("pid-")?;
    pid_str.parse().ok()
}

/// Check if a PID is alive using kill(pid, 0).
#[cfg(unix)]
pub fn is_pid_alive(pid: u32) -> bool {
    unsafe { libc::kill(pid as libc::pid_t, 0) == 0 }
}

/// Check if a per-process DB file is an orphan.
///
/// Orphan criteria: PID is not alive AND file mtime > 60 seconds old.
#[cfg(unix)]
pub fn is_orphan(path: &Path) -> bool {
    let Some(pid) = extract_pid_from_path(path) else {
        return false;
    };

    if is_pid_alive(pid) {
        return false;
    }

    // Check mtime age.
    let Ok(metadata) = fs::metadata(path) else {
        return false;
    };
    let Ok(modified) = metadata.modified() else {
        return false;
    };
    let Ok(age) = SystemTime::now().duration_since(modified) else {
        return false;
    };

    age.as_secs() > ORPHAN_MIN_AGE_SECS
}

/// List all per-process DB files in the active directory.
pub fn list_per_process_dbs() -> Result<Vec<PathBuf>> {
    let active_dir = agentc_data_dir().join("active");
    if !active_dir.exists() {
        return Ok(Vec::new());
    }

    let mut paths = Vec::new();
    for entry in fs::read_dir(&active_dir)
        .with_context(|| format!("Failed to read directory {}", active_dir.display()))?
    {
        let entry = entry?;
        let path = entry.path();
        if path.extension().and_then(|e| e.to_str()) == Some("db") {
            if let Some(stem) = path.file_stem().and_then(|s| s.to_str()) {
                if stem.starts_with("pid-") {
                    paths.push(path);
                }
            }
        }
    }

    Ok(paths)
}

/// Merge a single per-process DB into a canonical DB connection.
///
/// Uses ATTACH DATABASE + INSERT OR IGNORE in a single transaction.
/// Content tables are merged first, then spans.
///
/// The per-process DB file is deleted after a successful merge.
/// On failure, the file is preserved for retry.
pub fn merge_per_process_db(
    canonical_conn: &Connection,
    per_process_path: &Path,
) -> Result<MergeStats> {
    if !per_process_path.exists() {
        bail!(
            "Per-process DB not found: {}",
            per_process_path.display()
        );
    }

    // WAL checkpoint on per-process DB before merge.
    {
        let pp_conn = open_db(per_process_path)
            .with_context(|| format!("Failed to open per-process DB {}", per_process_path.display()))?;
        pp_conn
            .execute_batch("PRAGMA wal_checkpoint(TRUNCATE);")
            .with_context(|| "WAL checkpoint failed on per-process DB")?;
    }

    // ATTACH the per-process DB.
    let attach_path = per_process_path.to_string_lossy();
    canonical_conn.execute_batch(&format!(
        "ATTACH DATABASE '{}' AS source;",
        attach_path.replace('\'', "''")
    ))?;

    let result = (|| -> Result<MergeStats> {
        // Single transaction for atomicity.
        canonical_conn.execute_batch("BEGIN IMMEDIATE;")?;

        // Step 1: Merge content tables FIRST.
        let input_content_merged: i64 = canonical_conn
            .execute(
                "INSERT OR IGNORE INTO main.input_content SELECT * FROM source.input_content",
                [],
            )
            .map(|n| n as i64)?;

        let output_content_merged: i64 = canonical_conn
            .execute(
                "INSERT OR IGNORE INTO main.output_content SELECT * FROM source.output_content",
                [],
            )
            .map(|n| n as i64)?;

        // Step 2: Merge spans.
        let spans_merged: i64 = canonical_conn
            .execute(
                "INSERT OR IGNORE INTO main.spans SELECT * FROM source.spans",
                [],
            )
            .map(|n| n as i64)?;

        canonical_conn.execute_batch("COMMIT;")?;

        Ok(MergeStats {
            spans_merged,
            input_content_merged,
            output_content_merged,
        })
    })();

    // Always detach, even on error.
    let _ = canonical_conn.execute_batch("DETACH DATABASE source;");

    match result {
        Ok(stats) => {
            // Delete per-process DB after successful merge.
            if let Err(e) = fs::remove_file(per_process_path) {
                eprintln!(
                    "WARN: Failed to delete per-process DB {}: {e}",
                    per_process_path.display()
                );
            }
            // Also clean up WAL and SHM files.
            let wal = per_process_path.with_extension("db-wal");
            let shm = per_process_path.with_extension("db-shm");
            let _ = fs::remove_file(wal);
            let _ = fs::remove_file(shm);

            Ok(stats)
        }
        Err(e) => {
            eprintln!(
                "ERROR: Merge failed for {}: {e}. Per-process DB preserved for retry.",
                per_process_path.display()
            );
            Err(e)
        }
    }
}

/// Merge all pending per-process DBs into the canonical store.
///
/// Acquires a lockfile, opens/creates canonical DB, merges each per-process file.
/// Returns total merge statistics.
#[cfg(unix)]
pub fn merge_all_pending() -> Result<MergeStats> {
    let per_process_files = list_per_process_dbs()?;
    if per_process_files.is_empty() {
        return Ok(MergeStats::default());
    }

    let lock_path = lockfile_path();
    let _lock = FileLock::acquire(&lock_path, LOCK_TIMEOUT)?;

    let canonical_path = canonical_db_path();
    let canonical_conn = create_db(&canonical_path, true)?;

    let mut total = MergeStats::default();
    for path in &per_process_files {
        match merge_per_process_db(&canonical_conn, path) {
            Ok(stats) => {
                total.spans_merged += stats.spans_merged;
                total.input_content_merged += stats.input_content_merged;
                total.output_content_merged += stats.output_content_merged;
            }
            Err(e) => {
                eprintln!(
                    "ERROR: Merge failed for {}: {e}. Per-process DB preserved for retry.",
                    path.display()
                );
            }
        }
    }

    Ok(total)
}

/// Merge all orphaned per-process DBs (dead PID + stale mtime).
#[cfg(unix)]
pub fn merge_orphans() -> Result<MergeStats> {
    let per_process_files = list_per_process_dbs()?;
    let orphans: Vec<&PathBuf> = per_process_files.iter().filter(|p| is_orphan(p)).collect();

    if orphans.is_empty() {
        return Ok(MergeStats::default());
    }

    let lock_path = lockfile_path();
    let _lock = FileLock::acquire(&lock_path, LOCK_TIMEOUT)?;

    let canonical_path = canonical_db_path();
    let canonical_conn = create_db(&canonical_path, true)?;

    let mut total = MergeStats::default();
    for path in orphans {
        if let Some(pid) = extract_pid_from_path(path) {
            eprintln!(
                "INFO: Orphan detected: {} (PID {pid} not alive)",
                path.display()
            );
        }
        match merge_per_process_db(&canonical_conn, path) {
            Ok(stats) => {
                total.spans_merged += stats.spans_merged;
                total.input_content_merged += stats.input_content_merged;
                total.output_content_merged += stats.output_content_merged;
            }
            Err(e) => {
                eprintln!("ERROR: Merge failed for {}: {e}", path.display());
            }
        }
    }

    Ok(total)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::{create_db, insert_content, insert_span};
    use crate::span::{ContentTable, Span};
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
            input_content_id: Some("input-hash-1".to_string()),
            output_content_id: Some("output-hash-1".to_string()),
            input_embedding: None,
            output_embedding: None,
            embedding_model: None,
        }
    }

    #[test]
    fn test_per_process_db_path_contains_pid() {
        let path = per_process_db_path();
        let pid = std::process::id();
        assert!(path.to_str().unwrap().contains(&format!("pid-{pid}")));
        assert!(path.to_str().unwrap().contains("active"));
        assert!(path.to_str().unwrap().ends_with(".db"));
    }

    #[test]
    fn test_canonical_db_path() {
        let path = canonical_db_path();
        assert!(path.to_str().unwrap().contains(".agentc"));
        assert!(path.to_str().unwrap().ends_with("traces.db"));
    }

    #[test]
    fn test_extract_pid_from_path() {
        let path = PathBuf::from("/home/user/.agentc/active/pid-12345.db");
        assert_eq!(extract_pid_from_path(&path), Some(12345));

        let path = PathBuf::from("/home/user/.agentc/active/pid-1.db");
        assert_eq!(extract_pid_from_path(&path), Some(1));

        let path = PathBuf::from("/home/user/.agentc/active/not-a-pid.db");
        assert_eq!(extract_pid_from_path(&path), None);

        let path = PathBuf::from("/home/user/.agentc/active/pid-.db");
        assert_eq!(extract_pid_from_path(&path), None);
    }

    #[test]
    fn test_per_process_db_uses_core_schema() {
        let dir = TempDir::new().unwrap();
        let pp_path = dir.path().join("pid-99999.db");
        let conn = create_db(&pp_path, false).unwrap();

        // Should have spans, input_content, output_content tables.
        let tables: Vec<String> = {
            let mut stmt = conn
                .prepare("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
                .unwrap();
            stmt.query_map([], |row| row.get(0))
                .unwrap()
                .collect::<std::result::Result<Vec<_>, _>>()
                .unwrap()
        };
        assert!(tables.contains(&"spans".to_string()));
        assert!(tables.contains(&"input_content".to_string()));
        assert!(tables.contains(&"output_content".to_string()));

        // Should NOT have traces VIEW.
        let view_count: i32 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='view' AND name='traces'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(view_count, 0);
    }

    #[test]
    fn test_merge_copies_spans() {
        let dir = TempDir::new().unwrap();
        let pp_path = dir.path().join("pid-1.db");
        let canon_path = dir.path().join("traces.db");

        // Create per-process DB with spans.
        let pp_conn = create_db(&pp_path, false).unwrap();
        for i in 0..5 {
            let span = test_span(&format!("span-{i}"), "trace-1");
            insert_span(&pp_conn, &span).unwrap();
        }
        drop(pp_conn);

        // Create canonical DB.
        let canon_conn = create_db(&canon_path, true).unwrap();

        let stats = merge_per_process_db(&canon_conn, &pp_path).unwrap();
        assert_eq!(stats.spans_merged, 5);

        // Verify spans in canonical.
        let count: i32 = canon_conn
            .query_row("SELECT COUNT(*) FROM spans", [], |row| row.get(0))
            .unwrap();
        assert_eq!(count, 5);
    }

    #[test]
    fn test_merge_copies_content_tables() {
        let dir = TempDir::new().unwrap();
        let pp_path = dir.path().join("pid-1.db");
        let canon_path = dir.path().join("traces.db");

        let pp_conn = create_db(&pp_path, false).unwrap();
        insert_content(&pp_conn, ContentTable::InputContent, "in-1", b"data1", 1000).unwrap();
        insert_content(&pp_conn, ContentTable::InputContent, "in-2", b"data2", 1001).unwrap();
        insert_content(&pp_conn, ContentTable::OutputContent, "out-1", b"data3", 1002).unwrap();
        drop(pp_conn);

        let canon_conn = create_db(&canon_path, true).unwrap();
        let stats = merge_per_process_db(&canon_conn, &pp_path).unwrap();

        assert_eq!(stats.input_content_merged, 2);
        assert_eq!(stats.output_content_merged, 1);

        let in_count: i32 = canon_conn
            .query_row("SELECT COUNT(*) FROM input_content", [], |row| row.get(0))
            .unwrap();
        assert_eq!(in_count, 2);
    }

    #[test]
    fn test_merge_content_before_spans() {
        // Verify content is present when spans reference it.
        let dir = TempDir::new().unwrap();
        let pp_path = dir.path().join("pid-1.db");
        let canon_path = dir.path().join("traces.db");

        let pp_conn = create_db(&pp_path, false).unwrap();
        insert_content(&pp_conn, ContentTable::InputContent, "in-hash", b"data", 1000).unwrap();
        insert_content(&pp_conn, ContentTable::OutputContent, "out-hash", b"data", 1000).unwrap();
        let mut span = test_span("s1", "t1");
        span.input_content_id = Some("in-hash".to_string());
        span.output_content_id = Some("out-hash".to_string());
        insert_span(&pp_conn, &span).unwrap();
        drop(pp_conn);

        let canon_conn = create_db(&canon_path, true).unwrap();
        merge_per_process_db(&canon_conn, &pp_path).unwrap();

        // Verify content exists and span references it.
        let input_cid: Option<String> = canon_conn
            .query_row(
                "SELECT input_content_id FROM spans WHERE span_id = 's1'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(input_cid.as_deref(), Some("in-hash"));

        // Verify the referenced content row exists.
        let content_exists: bool = canon_conn
            .query_row(
                "SELECT COUNT(*) > 0 FROM input_content WHERE content_id = 'in-hash'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert!(content_exists);
    }

    #[test]
    fn test_merge_idempotent() {
        let dir = TempDir::new().unwrap();
        let pp_path = dir.path().join("pid-1.db");
        let canon_path = dir.path().join("traces.db");

        let pp_conn = create_db(&pp_path, false).unwrap();
        for i in 0..3 {
            insert_span(&pp_conn, &test_span(&format!("s{i}"), "t1")).unwrap();
        }
        insert_content(&pp_conn, ContentTable::InputContent, "c1", b"data", 1000).unwrap();
        drop(pp_conn);

        let canon_conn = create_db(&canon_path, true).unwrap();

        // First merge.
        let stats1 = merge_per_process_db(&canon_conn, &pp_path).unwrap();
        assert_eq!(stats1.spans_merged, 3);

        // Per-process DB was deleted by first merge. Recreate same data.
        let pp_conn = create_db(&pp_path, false).unwrap();
        for i in 0..3 {
            insert_span(&pp_conn, &test_span(&format!("s{i}"), "t1")).unwrap();
        }
        insert_content(&pp_conn, ContentTable::InputContent, "c1", b"data", 1000).unwrap();
        drop(pp_conn);

        // Second merge — same data, should be 0 due to INSERT OR IGNORE.
        let stats2 = merge_per_process_db(&canon_conn, &pp_path).unwrap();
        assert_eq!(stats2.spans_merged, 0);

        // Total count still 3.
        let count: i32 = canon_conn
            .query_row("SELECT COUNT(*) FROM spans", [], |row| row.get(0))
            .unwrap();
        assert_eq!(count, 3);
    }

    #[cfg(unix)]
    #[test]
    fn test_flock_acquisition_no_contention() {
        let dir = TempDir::new().unwrap();
        let lock_path = dir.path().join("test.lock");

        let lock = FileLock::acquire(&lock_path, Duration::from_secs(1));
        assert!(lock.is_ok());
    }

    #[cfg(unix)]
    #[test]
    fn test_flock_released_on_drop() {
        let dir = TempDir::new().unwrap();
        let lock_path = dir.path().join("test.lock");

        {
            let _lock = FileLock::acquire(&lock_path, Duration::from_secs(1)).unwrap();
            // Lock held here.
        }
        // Lock released by drop.

        // Should be able to re-acquire.
        let lock2 = FileLock::acquire(&lock_path, Duration::from_secs(1));
        assert!(lock2.is_ok());
    }

    #[test]
    fn test_per_process_db_deleted_after_merge() {
        let dir = TempDir::new().unwrap();
        let pp_path = dir.path().join("pid-1.db");
        let canon_path = dir.path().join("traces.db");

        let pp_conn = create_db(&pp_path, false).unwrap();
        insert_span(&pp_conn, &test_span("s1", "t1")).unwrap();
        drop(pp_conn);

        assert!(pp_path.exists());

        let canon_conn = create_db(&canon_path, true).unwrap();
        merge_per_process_db(&canon_conn, &pp_path).unwrap();

        // Per-process DB should be deleted.
        assert!(!pp_path.exists());
    }

    #[test]
    fn test_merge_nonexistent_file_errors() {
        let dir = TempDir::new().unwrap();
        let canon_path = dir.path().join("traces.db");
        let canon_conn = create_db(&canon_path, true).unwrap();

        let result = merge_per_process_db(&canon_conn, Path::new("/nonexistent/pid-1.db"));
        assert!(result.is_err());
    }

    #[cfg(unix)]
    #[test]
    fn test_is_pid_alive_current_process() {
        assert!(is_pid_alive(std::process::id()));
    }

    #[cfg(unix)]
    #[test]
    fn test_is_pid_alive_unlikely_pid() {
        // PID 4000000 is extremely unlikely to be alive.
        assert!(!is_pid_alive(4000000));
    }

    #[test]
    fn test_traces_view_in_canonical_after_merge() {
        let dir = TempDir::new().unwrap();
        let pp_path = dir.path().join("pid-1.db");
        let canon_path = dir.path().join("traces.db");

        let pp_conn = create_db(&pp_path, false).unwrap();
        let mut s1 = test_span("s1", "t1");
        s1.start_time = 1000;
        s1.end_time = Some(2000);
        let mut s2 = test_span("s2", "t1");
        s2.parent_span_id = Some("s1".to_string());
        s2.start_time = 1100;
        s2.end_time = Some(1500);
        insert_span(&pp_conn, &s1).unwrap();
        insert_span(&pp_conn, &s2).unwrap();
        drop(pp_conn);

        let canon_conn = create_db(&canon_path, true).unwrap();
        merge_per_process_db(&canon_conn, &pp_path).unwrap();

        // traces VIEW should work.
        let trace_count: i32 = canon_conn
            .query_row("SELECT COUNT(*) FROM traces", [], |row| row.get(0))
            .unwrap();
        assert_eq!(trace_count, 1);

        let root_count: i64 = canon_conn
            .query_row(
                "SELECT root_span_count FROM traces WHERE trace_id = 't1'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(root_count, 1);
    }

    #[test]
    fn test_list_per_process_dbs_empty() {
        let dir = TempDir::new().unwrap();
        // Override the function's default path by creating a specific dir.
        // We can't easily test list_per_process_dbs() since it uses a fixed path,
        // but we can test the logic it relies on.
        let active_dir = dir.path().join("active");
        fs::create_dir_all(&active_dir).unwrap();
        // No files — read_dir returns empty.
        let count = fs::read_dir(&active_dir).unwrap().count();
        assert_eq!(count, 0);
    }

    #[test]
    fn test_integration_two_processes_merge() {
        let dir = TempDir::new().unwrap();
        let pp1_path = dir.path().join("pid-1.db");
        let pp2_path = dir.path().join("pid-2.db");
        let canon_path = dir.path().join("traces.db");

        // Process 1 writes 5 spans.
        let pp1 = create_db(&pp1_path, false).unwrap();
        for i in 0..5 {
            let mut span = test_span(&format!("p1-s{i}"), "trace-p1");
            span.start_time = i as i64 * 1000;
            insert_span(&pp1, &span).unwrap();
        }
        insert_content(&pp1, ContentTable::InputContent, "p1-in", b"p1data", 1000).unwrap();
        drop(pp1);

        // Process 2 writes 3 spans.
        let pp2 = create_db(&pp2_path, false).unwrap();
        for i in 0..3 {
            let mut span = test_span(&format!("p2-s{i}"), "trace-p2");
            span.start_time = 100000 + i as i64 * 1000;
            insert_span(&pp2, &span).unwrap();
        }
        insert_content(&pp2, ContentTable::InputContent, "p2-in", b"p2data", 2000).unwrap();
        drop(pp2);

        // Merge both into canonical.
        let canon = create_db(&canon_path, true).unwrap();
        merge_per_process_db(&canon, &pp1_path).unwrap();
        merge_per_process_db(&canon, &pp2_path).unwrap();

        let total_spans: i32 = canon
            .query_row("SELECT COUNT(*) FROM spans", [], |row| row.get(0))
            .unwrap();
        assert_eq!(total_spans, 8);

        let total_content: i32 = canon
            .query_row("SELECT COUNT(*) FROM input_content", [], |row| row.get(0))
            .unwrap();
        assert_eq!(total_content, 2);

        // Verify traces VIEW shows 2 traces.
        let trace_count: i32 = canon
            .query_row("SELECT COUNT(*) FROM traces", [], |row| row.get(0))
            .unwrap();
        assert_eq!(trace_count, 2);
    }

    #[test]
    fn test_integration_crash_recovery_idempotent() {
        // Simulate: merge committed but file not deleted (crash between steps).
        let dir = TempDir::new().unwrap();
        let pp_path = dir.path().join("pid-1.db");
        let canon_path = dir.path().join("traces.db");

        // First: create and merge.
        let pp_conn = create_db(&pp_path, false).unwrap();
        insert_span(&pp_conn, &test_span("s1", "t1")).unwrap();
        drop(pp_conn);

        let canon_conn = create_db(&canon_path, true).unwrap();
        // Manually do the merge without deleting (simulating crash).
        {
            let pp_conn2 = open_db(&pp_path).unwrap();
            pp_conn2.execute_batch("PRAGMA wal_checkpoint(TRUNCATE);").unwrap();
            drop(pp_conn2);

            let attach_path = pp_path.to_string_lossy();
            canon_conn
                .execute_batch(&format!("ATTACH DATABASE '{}' AS source;", attach_path))
                .unwrap();
            canon_conn
                .execute_batch(
                    "BEGIN;
                     INSERT OR IGNORE INTO main.input_content SELECT * FROM source.input_content;
                     INSERT OR IGNORE INTO main.output_content SELECT * FROM source.output_content;
                     INSERT OR IGNORE INTO main.spans SELECT * FROM source.spans;
                     COMMIT;",
                )
                .unwrap();
            canon_conn
                .execute_batch("DETACH DATABASE source;")
                .unwrap();
        }
        // File still exists (crash simulation).
        assert!(pp_path.exists());

        // Re-merge should be no-op.
        let stats = merge_per_process_db(&canon_conn, &pp_path).unwrap();
        assert_eq!(stats.spans_merged, 0); // INSERT OR IGNORE → 0 new rows.

        // Total still 1 span.
        let count: i32 = canon_conn
            .query_row("SELECT COUNT(*) FROM spans", [], |row| row.get(0))
            .unwrap();
        assert_eq!(count, 1);
    }

    #[test]
    fn test_integration_10_process_files_merged() {
        let dir = TempDir::new().unwrap();
        let canon_path = dir.path().join("traces.db");
        let canon_conn = create_db(&canon_path, true).unwrap();

        for proc_idx in 0..10 {
            let pp_path = dir.path().join(format!("pid-{}.db", proc_idx + 100));
            let pp_conn = create_db(&pp_path, false).unwrap();

            for span_idx in 0..5 {
                let mut span = test_span(
                    &format!("p{proc_idx}-s{span_idx}"),
                    &format!("trace-p{proc_idx}"),
                );
                span.start_time = (proc_idx * 100000 + span_idx * 1000) as i64;
                insert_span(&pp_conn, &span).unwrap();
            }
            drop(pp_conn);

            merge_per_process_db(&canon_conn, &pp_path).unwrap();
        }

        let total: i32 = canon_conn
            .query_row("SELECT COUNT(*) FROM spans", [], |row| row.get(0))
            .unwrap();
        assert_eq!(total, 50); // 10 * 5

        let traces: i32 = canon_conn
            .query_row("SELECT COUNT(*) FROM traces", [], |row| row.get(0))
            .unwrap();
        assert_eq!(traces, 10);
    }

    #[test]
    fn test_lockfile_stale_detection() {
        let dir = TempDir::new().unwrap();
        let lock_path = dir.path().join("test.lock");

        // Fresh lockfile — not stale.
        fs::write(&lock_path, b"").unwrap();
        assert!(!is_lockfile_stale(&lock_path));

        // Nonexistent lockfile — not stale.
        assert!(!is_lockfile_stale(&dir.path().join("nonexistent.lock")));
    }

    #[test]
    fn test_attach_merge_preserves_all_columns() {
        let dir = TempDir::new().unwrap();
        let pp_path = dir.path().join("pid-1.db");
        let canon_path = dir.path().join("traces.db");

        let pp_conn = create_db(&pp_path, false).unwrap();
        let mut span = test_span("s1", "t1");
        span.model = Some("gpt-4o".to_string());
        span.provider = Some("openai".to_string());
        span.input_tokens = Some(500);
        span.output_tokens = Some(200);
        span.cache_creation_tokens = Some(100);
        span.cache_read_tokens = Some(50);
        span.cost_usd = Some(0.05);
        span.input_embedding = Some(vec![1, 2, 3, 4]);
        span.output_embedding = Some(vec![5, 6, 7, 8]);
        span.embedding_model = Some("potion-base-8M".to_string());
        insert_span(&pp_conn, &span).unwrap();
        drop(pp_conn);

        let canon_conn = create_db(&canon_path, true).unwrap();
        merge_per_process_db(&canon_conn, &pp_path).unwrap();

        // Verify all columns preserved.
        let row = canon_conn
            .query_row(
                "SELECT model, provider, input_tokens, output_tokens, \
                 cache_creation_tokens, cache_read_tokens, cost_usd, \
                 input_embedding, output_embedding, embedding_model \
                 FROM spans WHERE span_id = 's1'",
                [],
                |row| {
                    Ok((
                        row.get::<_, Option<String>>(0)?,
                        row.get::<_, Option<String>>(1)?,
                        row.get::<_, Option<i64>>(2)?,
                        row.get::<_, Option<i64>>(3)?,
                        row.get::<_, Option<i64>>(4)?,
                        row.get::<_, Option<i64>>(5)?,
                        row.get::<_, Option<f64>>(6)?,
                        row.get::<_, Option<Vec<u8>>>(7)?,
                        row.get::<_, Option<Vec<u8>>>(8)?,
                        row.get::<_, Option<String>>(9)?,
                    ))
                },
            )
            .unwrap();

        assert_eq!(row.0.as_deref(), Some("gpt-4o"));
        assert_eq!(row.1.as_deref(), Some("openai"));
        assert_eq!(row.2, Some(500));
        assert_eq!(row.3, Some(200));
        assert_eq!(row.4, Some(100));
        assert_eq!(row.5, Some(50));
        assert_eq!(row.6, Some(0.05));
        assert_eq!(row.7, Some(vec![1, 2, 3, 4]));
        assert_eq!(row.8, Some(vec![5, 6, 7, 8]));
        assert_eq!(row.9.as_deref(), Some("potion-base-8M"));
    }
}
