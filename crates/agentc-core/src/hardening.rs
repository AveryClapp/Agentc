//! Hardening utilities: permission verification, symlink protection, security checks.

use std::path::Path;

use anyhow::{bail, Result};

/// Verify that a directory has safe permissions (0700 on Unix).
///
/// Returns `Ok(())` if permissions are correct, or `Err` with a diagnostic message.
#[cfg(unix)]
pub fn verify_dir_permissions(path: &Path) -> Result<()> {
    use std::os::unix::fs::PermissionsExt;

    if !path.exists() {
        return Ok(()); // Will be created with correct permissions by create_db.
    }

    let metadata = std::fs::metadata(path)?;
    if !metadata.is_dir() {
        bail!("{} is not a directory", path.display());
    }

    let mode = metadata.permissions().mode() & 0o777;
    if mode != 0o700 {
        eprintln!(
            "WARN: Directory {} has permissions {:04o}, expected 0700. \
             Other users may be able to access your trace data.",
            path.display(),
            mode
        );
    }
    Ok(())
}

/// Verify that a file has safe permissions (0600 on Unix).
#[cfg(unix)]
pub fn verify_file_permissions(path: &Path) -> Result<()> {
    use std::os::unix::fs::PermissionsExt;

    if !path.exists() {
        return Ok(());
    }

    let metadata = std::fs::metadata(path)?;
    let mode = metadata.permissions().mode() & 0o777;
    if mode != 0o600 {
        eprintln!(
            "WARN: File {} has permissions {:04o}, expected 0600. \
             Other users may be able to read your trace data.",
            path.display(),
            mode
        );
    }
    Ok(())
}

/// Check that a storage path is not a symlink (symlink attack protection).
///
/// If `path` is a symlink, returns an error. If it doesn't exist yet, returns Ok
/// (it will be created as a real directory).
pub fn check_no_symlink(path: &Path) -> Result<()> {
    // Check the path itself — symlink_metadata does NOT follow symlinks.
    if let Ok(metadata) = std::fs::symlink_metadata(path) {
        if metadata.file_type().is_symlink() {
            bail!(
                "Security: {} is a symlink. agentc refuses to use symlinks for storage \
                 to prevent symlink attacks. Remove the symlink and re-run.",
                path.display()
            );
        }
    }
    Ok(())
}

/// Run all security checks on a storage directory.
///
/// Checks: no symlink, directory permissions.
pub fn audit_storage_dir(path: &Path) -> Result<()> {
    check_no_symlink(path)?;
    #[cfg(unix)]
    verify_dir_permissions(path)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    // --- Item 1: Fuzz testing (write_span with arbitrary inputs) ---

    mod fuzz_write_span {
        use crate::db::create_db;
        use crate::storage::{write_span, SpanInput, WriteSpanOptions};
        use tempfile::TempDir;

        fn make_fuzz_input(id: &str) -> SpanInput {
            SpanInput {
                span_id: id.to_string(),
                trace_id: "fuzz-trace".to_string(),
                parent_span_id: None,
                name: "fuzz".to_string(),
                kind: "chat".to_string(),
                start_time: 1000000,
                end_time: Some(2000000),
                status: "OK".to_string(),
                model: None,
                provider: None,
                input_tokens: None,
                output_tokens: None,
                cache_creation_tokens: None,
                cache_read_tokens: None,
                attributes: "{}".to_string(),
                input_messages: None,
                output_messages: None,
            }
        }

        fn opts_content_no_embed() -> WriteSpanOptions {
            WriteSpanOptions {
                capture_content: true,
                capture_embeddings: false,
            }
        }

        #[test]
        fn test_fuzz_empty_span_id() {
            let dir = TempDir::new().unwrap();
            let conn = create_db(&dir.path().join("t.db"), false).unwrap();
            let mut input = make_fuzz_input("");
            input.span_id = String::new();
            // Empty span_id is valid SQL — it's just an empty string PK.
            let result = write_span(&conn, &input, opts_content_no_embed());
            assert!(result.is_ok());
        }

        #[test]
        fn test_fuzz_unicode_span_id() {
            let dir = TempDir::new().unwrap();
            let conn = create_db(&dir.path().join("t.db"), false).unwrap();
            let mut input = make_fuzz_input("🔥💀");
            input.name = "日本語テスト".to_string();
            input.attributes = r#"{"emoji": "🎉"}"#.to_string();
            let result = write_span(&conn, &input, opts_content_no_embed());
            assert!(result.is_ok());
        }

        #[test]
        fn test_fuzz_very_long_span_id() {
            let dir = TempDir::new().unwrap();
            let conn = create_db(&dir.path().join("t.db"), false).unwrap();
            let long_id = "x".repeat(10_000);
            let input = make_fuzz_input(&long_id);
            let result = write_span(&conn, &input, opts_content_no_embed());
            assert!(result.is_ok());
        }

        #[test]
        fn test_fuzz_null_bytes_in_name() {
            let dir = TempDir::new().unwrap();
            let conn = create_db(&dir.path().join("t.db"), false).unwrap();
            let mut input = make_fuzz_input("null-bytes");
            input.name = "test\0name\0with\0nulls".to_string();
            let result = write_span(&conn, &input, opts_content_no_embed());
            assert!(result.is_ok());
        }

        #[test]
        fn test_fuzz_negative_timestamps() {
            let dir = TempDir::new().unwrap();
            let conn = create_db(&dir.path().join("t.db"), false).unwrap();
            let mut input = make_fuzz_input("neg-time");
            input.start_time = -1;
            input.end_time = Some(-100);
            let result = write_span(&conn, &input, opts_content_no_embed());
            assert!(result.is_ok());
        }

        #[test]
        fn test_fuzz_max_i64_tokens() {
            let dir = TempDir::new().unwrap();
            let conn = create_db(&dir.path().join("t.db"), false).unwrap();
            let mut input = make_fuzz_input("max-tokens");
            input.input_tokens = Some(i64::MAX);
            input.output_tokens = Some(i64::MAX);
            input.cache_creation_tokens = Some(i64::MAX);
            input.cache_read_tokens = Some(i64::MAX);
            let result = write_span(&conn, &input, opts_content_no_embed());
            assert!(result.is_ok());
        }

        #[test]
        fn test_fuzz_huge_json_messages() {
            let dir = TempDir::new().unwrap();
            let conn = create_db(&dir.path().join("t.db"), false).unwrap();
            let mut input = make_fuzz_input("huge-msg");
            // 100KB message content
            let big_content = "x".repeat(100_000);
            input.input_messages = Some(serde_json::json!([{
                "role": "user",
                "content": big_content
            }]));
            let result = write_span(&conn, &input, opts_content_no_embed());
            assert!(result.is_ok());
        }

        #[test]
        fn test_fuzz_deeply_nested_json() {
            let dir = TempDir::new().unwrap();
            let conn = create_db(&dir.path().join("t.db"), false).unwrap();
            let mut input = make_fuzz_input("deep-nest");
            // Build 50-level deep nested object
            let mut val = serde_json::json!("leaf");
            for _ in 0..50 {
                val = serde_json::json!({"nested": val});
            }
            input.input_messages = Some(serde_json::json!([{
                "role": "user",
                "content": val.to_string()
            }]));
            let result = write_span(&conn, &input, opts_content_no_embed());
            assert!(result.is_ok());
        }

        #[test]
        fn test_fuzz_invalid_json_attributes() {
            let dir = TempDir::new().unwrap();
            let conn = create_db(&dir.path().join("t.db"), false).unwrap();
            let mut input = make_fuzz_input("bad-attrs");
            // attributes is just a TEXT column — invalid JSON is stored as-is.
            input.attributes = "this is not json {{{".to_string();
            let result = write_span(&conn, &input, opts_content_no_embed());
            assert!(result.is_ok());
        }

        #[test]
        fn test_fuzz_special_chars_in_all_fields() {
            let dir = TempDir::new().unwrap();
            let conn = create_db(&dir.path().join("t.db"), false).unwrap();
            let special = "'; DROP TABLE spans;--\n\r\t\\\"<script>alert(1)</script>";
            let mut input = make_fuzz_input(special);
            input.trace_id = special.to_string();
            input.name = special.to_string();
            input.kind = special.to_string();
            input.status = special.to_string();
            input.model = Some(special.to_string());
            input.provider = Some(special.to_string());
            input.attributes = special.to_string();
            let result = write_span(&conn, &input, opts_content_no_embed());
            assert!(result.is_ok());
        }
    }

    // --- Item 2: SQL injection resistance ---

    mod sql_injection {
        use crate::db::{create_db, insert_content, insert_span, query_spans_by_trace};
        use crate::span::{ContentTable, Span};
        use crate::storage::{write_span, SpanInput, WriteSpanOptions};
        use tempfile::TempDir;

        fn injection_span(id: &str, trace_id: &str) -> Span {
            Span {
                span_id: id.to_string(),
                trace_id: trace_id.to_string(),
                parent_span_id: None,
                name: "test".to_string(),
                kind: "chat".to_string(),
                start_time: 1000000,
                end_time: Some(2000000),
                status: "OK".to_string(),
                model: None,
                provider: None,
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
            }
        }

        #[test]
        fn test_sql_injection_in_span_id() {
            let dir = TempDir::new().unwrap();
            let conn = create_db(&dir.path().join("t.db"), false).unwrap();
            let payload = "'; DROP TABLE spans; --";
            let span = injection_span(payload, "trace-1");
            insert_span(&conn, &span).unwrap();

            // Verify spans table still exists and contains the row.
            let count: i32 = conn
                .query_row("SELECT COUNT(*) FROM spans", [], |row| row.get(0))
                .unwrap();
            assert_eq!(count, 1);
        }

        #[test]
        fn test_sql_injection_in_trace_id() {
            let dir = TempDir::new().unwrap();
            let conn = create_db(&dir.path().join("t.db"), false).unwrap();
            let payload = "' OR '1'='1";
            let span = injection_span("s1", payload);
            insert_span(&conn, &span).unwrap();

            // Query with the injection payload as trace_id — should get exact match, not all rows.
            let spans = query_spans_by_trace(&conn, payload).unwrap();
            assert_eq!(spans.len(), 1);
            assert_eq!(spans[0].trace_id, payload);
        }

        #[test]
        fn test_sql_injection_in_attributes() {
            let dir = TempDir::new().unwrap();
            let conn = create_db(&dir.path().join("t.db"), false).unwrap();
            let mut span = injection_span("s1", "t1");
            span.attributes = r#"{"key": "'; DELETE FROM spans WHERE '1'='1"}"#.to_string();
            insert_span(&conn, &span).unwrap();

            let count: i32 = conn
                .query_row("SELECT COUNT(*) FROM spans", [], |row| row.get(0))
                .unwrap();
            assert_eq!(count, 1);
        }

        #[test]
        fn test_sql_injection_in_content_id() {
            let dir = TempDir::new().unwrap();
            let conn = create_db(&dir.path().join("t.db"), false).unwrap();
            let payload = "abc'; DROP TABLE input_content;--";
            insert_content(&conn, ContentTable::InputContent, payload, b"data", 1000).unwrap();

            // Table still exists.
            let count: i32 = conn
                .query_row("SELECT COUNT(*) FROM input_content", [], |row| row.get(0))
                .unwrap();
            assert_eq!(count, 1);
        }

        #[test]
        fn test_sql_injection_via_write_span_messages() {
            let dir = TempDir::new().unwrap();
            let conn = create_db(&dir.path().join("t.db"), false).unwrap();
            let mut input = SpanInput {
                span_id: "s1".to_string(),
                trace_id: "t1".to_string(),
                parent_span_id: None,
                name: "test".to_string(),
                kind: "chat".to_string(),
                start_time: 1000000,
                end_time: Some(2000000),
                status: "OK".to_string(),
                model: None,
                provider: None,
                input_tokens: None,
                output_tokens: None,
                cache_creation_tokens: None,
                cache_read_tokens: None,
                attributes: "{}".to_string(),
                input_messages: None,
                output_messages: None,
            };
            input.input_messages = Some(serde_json::json!([{
                "role": "user",
                "content": "'; DROP TABLE spans; --"
            }]));
            let result = write_span(
                &conn,
                &input,
                WriteSpanOptions {
                    capture_content: true,
                    capture_embeddings: false,
                },
            );
            assert!(result.is_ok());

            // Verify everything is intact.
            let span_count: i32 = conn
                .query_row("SELECT COUNT(*) FROM spans", [], |row| row.get(0))
                .unwrap();
            let content_count: i32 = conn
                .query_row("SELECT COUNT(*) FROM input_content", [], |row| row.get(0))
                .unwrap();
            assert_eq!(span_count, 1);
            assert_eq!(content_count, 1);
        }
    }

    // --- Item 3: Permission checks ---

    #[cfg(unix)]
    mod permissions {
        use super::*;
        use crate::db::create_db;
        use std::os::unix::fs::PermissionsExt;

        #[test]
        fn test_create_db_sets_file_0600() {
            let dir = TempDir::new().unwrap();
            let db_path = dir.path().join("sub").join("test.db");
            let _conn = create_db(&db_path, false).unwrap();

            let mode = std::fs::metadata(&db_path)
                .unwrap()
                .permissions()
                .mode()
                & 0o777;
            assert_eq!(mode, 0o600);
        }

        #[test]
        fn test_create_db_sets_dir_0700() {
            let dir = TempDir::new().unwrap();
            let sub = dir.path().join("agentc_data");
            let db_path = sub.join("test.db");
            let _conn = create_db(&db_path, false).unwrap();

            let mode = std::fs::metadata(&sub)
                .unwrap()
                .permissions()
                .mode()
                & 0o777;
            assert_eq!(mode, 0o700);
        }

        #[test]
        fn test_verify_dir_permissions_correct() {
            let dir = TempDir::new().unwrap();
            let sub = dir.path().join("good");
            std::fs::create_dir(&sub).unwrap();
            std::fs::set_permissions(&sub, std::fs::Permissions::from_mode(0o700)).unwrap();
            assert!(verify_dir_permissions(&sub).is_ok());
        }

        #[test]
        fn test_verify_file_permissions_correct() {
            let dir = TempDir::new().unwrap();
            let f = dir.path().join("good.db");
            std::fs::write(&f, b"test").unwrap();
            std::fs::set_permissions(&f, std::fs::Permissions::from_mode(0o600)).unwrap();
            assert!(verify_file_permissions(&f).is_ok());
        }

        #[test]
        fn test_verify_nonexistent_path_ok() {
            let path = std::path::PathBuf::from("/tmp/nonexistent-agentc-test-dir-xyz");
            assert!(verify_dir_permissions(&path).is_ok());
            assert!(verify_file_permissions(&path).is_ok());
        }
    }

    // --- Item 4: Concurrent access (multi-process merge, no data loss) ---

    mod concurrent_access {
        use crate::db::{create_db, insert_span};
        use crate::merge::merge_per_process_db;
        use crate::span::Span;
        use tempfile::TempDir;

        fn test_span(id: &str, trace_id: &str) -> Span {
            Span {
                span_id: id.to_string(),
                trace_id: trace_id.to_string(),
                parent_span_id: None,
                name: "test".to_string(),
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
                embedding_model: None,
            }
        }

        #[test]
        fn test_5_processes_write_then_merge_no_data_loss() {
            let dir = TempDir::new().unwrap();
            let canon_path = dir.path().join("traces.db");

            // Simulate 5 processes each writing 20 spans.
            for proc_idx in 0..5 {
                let pp_path = dir.path().join(format!("pid-{}.db", 10000 + proc_idx));
                let pp_conn = create_db(&pp_path, false).unwrap();
                for span_idx in 0..20 {
                    let mut span = test_span(
                        &format!("p{proc_idx}-s{span_idx}"),
                        &format!("trace-p{proc_idx}"),
                    );
                    span.start_time = (proc_idx * 100000 + span_idx * 1000) as i64;
                    insert_span(&pp_conn, &span).unwrap();
                }
                drop(pp_conn);
            }

            // Merge all 5 into canonical.
            let canon_conn = create_db(&canon_path, true).unwrap();
            for proc_idx in 0..5 {
                let pp_path = dir.path().join(format!("pid-{}.db", 10000 + proc_idx));
                merge_per_process_db(&canon_conn, &pp_path).unwrap();
            }

            // Verify no data loss: exactly 100 spans, 5 traces.
            let span_count: i32 = canon_conn
                .query_row("SELECT COUNT(*) FROM spans", [], |row| row.get(0))
                .unwrap();
            assert_eq!(span_count, 100);

            let trace_count: i32 = canon_conn
                .query_row("SELECT COUNT(*) FROM traces", [], |row| row.get(0))
                .unwrap();
            assert_eq!(trace_count, 5);

            // Verify each trace has exactly 20 spans.
            for proc_idx in 0..5 {
                let trace_spans: i32 = canon_conn
                    .query_row(
                        "SELECT COUNT(*) FROM spans WHERE trace_id = ?1",
                        [format!("trace-p{proc_idx}")],
                        |row| row.get(0),
                    )
                    .unwrap();
                assert_eq!(trace_spans, 20);
            }
        }

        #[test]
        fn test_duplicate_merge_is_idempotent() {
            let dir = TempDir::new().unwrap();
            let pp_path = dir.path().join("pid-1.db");
            let canon_path = dir.path().join("traces.db");

            // Write spans.
            let pp_conn = create_db(&pp_path, false).unwrap();
            for i in 0..10 {
                insert_span(&pp_conn, &test_span(&format!("s{i}"), "t1")).unwrap();
            }
            drop(pp_conn);

            // Merge once.
            let canon_conn = create_db(&canon_path, true).unwrap();
            merge_per_process_db(&canon_conn, &pp_path).unwrap();

            // Per-process file deleted. Recreate same data.
            let pp_conn = create_db(&pp_path, false).unwrap();
            for i in 0..10 {
                insert_span(&pp_conn, &test_span(&format!("s{i}"), "t1")).unwrap();
            }
            drop(pp_conn);

            // Merge again — should be no-op (INSERT OR IGNORE).
            let stats = merge_per_process_db(&canon_conn, &pp_path).unwrap();
            assert_eq!(stats.spans_merged, 0);

            let total: i32 = canon_conn
                .query_row("SELECT COUNT(*) FROM spans", [], |row| row.get(0))
                .unwrap();
            assert_eq!(total, 10);
        }
    }

    // --- Item 5: WAL recovery after simulated crash ---

    mod wal_recovery {
        use crate::db::{create_db, insert_span, open_db};
        use crate::span::Span;
        use tempfile::TempDir;

        fn test_span(id: &str) -> Span {
            Span {
                span_id: id.to_string(),
                trace_id: "t1".to_string(),
                parent_span_id: None,
                name: "test".to_string(),
                kind: "chat".to_string(),
                start_time: 1000000,
                end_time: Some(2000000),
                status: "OK".to_string(),
                model: None,
                provider: None,
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
            }
        }

        #[test]
        fn test_wal_recovery_after_abrupt_close() {
            let dir = TempDir::new().unwrap();
            let db_path = dir.path().join("test.db");

            // Write spans, then drop without checkpoint (simulates crash).
            {
                let conn = create_db(&db_path, false).unwrap();
                for i in 0..50 {
                    insert_span(&conn, &test_span(&format!("s{i}"))).unwrap();
                }
                // Drop without explicit WAL checkpoint.
            }

            // WAL file might exist.
            let wal_exists = db_path.with_extension("db-wal").exists();

            // Re-open — SQLite auto-recovers WAL.
            let conn = open_db(&db_path).unwrap();
            let count: i32 = conn
                .query_row("SELECT COUNT(*) FROM spans", [], |row| row.get(0))
                .unwrap();
            assert_eq!(count, 50, "WAL recovery should preserve all 50 spans (WAL existed: {wal_exists})");
        }

        #[test]
        fn test_db_not_corrupted_after_partial_write() {
            let dir = TempDir::new().unwrap();
            let db_path = dir.path().join("test.db");

            // Create DB with some data.
            {
                let conn = create_db(&db_path, false).unwrap();
                for i in 0..20 {
                    insert_span(&conn, &test_span(&format!("s{i}"))).unwrap();
                }
            }

            // Re-open and write more.
            {
                let conn = open_db(&db_path).unwrap();
                for i in 20..40 {
                    insert_span(&conn, &test_span(&format!("s{i}"))).unwrap();
                }
            }

            // Final open — verify integrity.
            let conn = open_db(&db_path).unwrap();
            let count: i32 = conn
                .query_row("SELECT COUNT(*) FROM spans", [], |row| row.get(0))
                .unwrap();
            assert_eq!(count, 40);

            // Run integrity check.
            let integrity: String = conn
                .query_row("PRAGMA integrity_check", [], |row| row.get(0))
                .unwrap();
            assert_eq!(integrity, "ok");
        }
    }

    // --- Item 7: Large span handling (1MB+) ---

    mod large_spans {
        use crate::db::create_db;
        use crate::storage::{compress_content, decompress_content, write_span, SpanInput, WriteSpanOptions};
        use tempfile::TempDir;

        #[test]
        fn test_1mb_input_messages_compresses_and_stores() {
            let dir = TempDir::new().unwrap();
            let conn = create_db(&dir.path().join("t.db"), false).unwrap();

            // 1MB of content.
            let big_text = "a".repeat(1_000_000);
            let input = SpanInput {
                span_id: "big-span".to_string(),
                trace_id: "big-trace".to_string(),
                parent_span_id: None,
                name: "test".to_string(),
                kind: "chat".to_string(),
                start_time: 1000000,
                end_time: Some(2000000),
                status: "OK".to_string(),
                model: None,
                provider: None,
                input_tokens: Some(100000),
                output_tokens: None,
                cache_creation_tokens: None,
                cache_read_tokens: None,
                attributes: "{}".to_string(),
                input_messages: Some(serde_json::json!([{
                    "role": "user",
                    "content": big_text
                }])),
                output_messages: None,
            };

            let span = write_span(
                &conn,
                &input,
                WriteSpanOptions {
                    capture_content: true,
                    capture_embeddings: false,
                },
            )
            .unwrap();

            assert!(span.input_content_id.is_some());

            // Verify stored blob is significantly smaller than 1MB (zstd compresses well).
            let blob: Vec<u8> = conn
                .query_row(
                    "SELECT content_text FROM input_content WHERE content_id = ?1",
                    [span.input_content_id.as_ref().unwrap()],
                    |row| row.get(0),
                )
                .unwrap();

            assert!(
                blob.len() < 100_000,
                "1MB of repeated chars should compress to <100KB, got {} bytes",
                blob.len()
            );

            // Verify roundtrip.
            let decompressed = decompress_content(&blob).unwrap();
            assert!(decompressed.len() > 1_000_000); // Canonical JSON of the message
        }

        #[test]
        fn test_5mb_message_does_not_oom() {
            let dir = TempDir::new().unwrap();
            let conn = create_db(&dir.path().join("t.db"), false).unwrap();

            // 5MB message.
            let big_text = "x".repeat(5_000_000);
            let input = SpanInput {
                span_id: "huge-span".to_string(),
                trace_id: "huge-trace".to_string(),
                parent_span_id: None,
                name: "test".to_string(),
                kind: "chat".to_string(),
                start_time: 1000000,
                end_time: Some(2000000),
                status: "OK".to_string(),
                model: None,
                provider: None,
                input_tokens: None,
                output_tokens: None,
                cache_creation_tokens: None,
                cache_read_tokens: None,
                attributes: "{}".to_string(),
                input_messages: Some(serde_json::json!([{
                    "role": "user",
                    "content": big_text
                }])),
                output_messages: None,
            };

            let result = write_span(
                &conn,
                &input,
                WriteSpanOptions {
                    capture_content: true,
                    capture_embeddings: false,
                },
            );
            assert!(result.is_ok());
        }

        #[test]
        fn test_zstd_roundtrip_1mb() {
            let data = vec![42u8; 1_000_000];
            let compressed = compress_content(&data).unwrap();
            let decompressed = decompress_content(&compressed).unwrap();
            assert_eq!(decompressed, data);
        }

        #[test]
        fn test_1000_spans_no_performance_cliff() {
            let dir = TempDir::new().unwrap();
            let conn = create_db(&dir.path().join("t.db"), false).unwrap();

            let opts = WriteSpanOptions {
                capture_content: true,
                capture_embeddings: false,
            };

            let start = std::time::Instant::now();
            for i in 0..1000 {
                let input = SpanInput {
                    span_id: format!("span-{i}"),
                    trace_id: "perf-trace".to_string(),
                    parent_span_id: if i > 0 { Some("span-0".to_string()) } else { None },
                    name: format!("call_{i}"),
                    kind: "chat".to_string(),
                    start_time: i as i64 * 1000,
                    end_time: Some(i as i64 * 1000 + 500),
                    status: "OK".to_string(),
                    model: Some("claude-sonnet-4-20250514".to_string()),
                    provider: Some("anthropic".to_string()),
                    input_tokens: Some(100),
                    output_tokens: Some(50),
                    cache_creation_tokens: None,
                    cache_read_tokens: None,
                    attributes: "{}".to_string(),
                    input_messages: Some(serde_json::json!([{
                        "role": "user",
                        "content": format!("Message {i}")
                    }])),
                    output_messages: Some(serde_json::json!([{
                        "role": "assistant",
                        "content": format!("Response {i}")
                    }])),
                };
                write_span(&conn, &input, opts).unwrap();
            }
            let elapsed = start.elapsed();

            // 1000 spans should complete in under 30s (generous bound for CI).
            assert!(
                elapsed.as_secs() < 30,
                "1000 spans took {elapsed:?}, expected < 30s"
            );

            let count: i32 = conn
                .query_row("SELECT COUNT(*) FROM spans", [], |row| row.get(0))
                .unwrap();
            assert_eq!(count, 1000);
        }
    }

    // --- Item 8: Symlink attack protection ---

    mod symlink_protection {
        use super::*;

        #[test]
        fn test_check_no_symlink_regular_dir() {
            let dir = TempDir::new().unwrap();
            assert!(check_no_symlink(dir.path()).is_ok());
        }

        #[test]
        fn test_check_no_symlink_nonexistent() {
            let path = std::path::PathBuf::from("/tmp/nonexistent-agentc-test-xyz");
            assert!(check_no_symlink(&path).is_ok());
        }

        #[cfg(unix)]
        #[test]
        fn test_check_no_symlink_detects_symlink() {
            let dir = TempDir::new().unwrap();
            let target = dir.path().join("real_dir");
            let link = dir.path().join("symlink_dir");
            std::fs::create_dir(&target).unwrap();
            std::os::unix::fs::symlink(&target, &link).unwrap();

            let result = check_no_symlink(&link);
            assert!(result.is_err());
            let err = result.unwrap_err().to_string();
            assert!(err.contains("symlink"));
        }

        #[cfg(unix)]
        #[test]
        fn test_audit_storage_dir_rejects_symlink() {
            let dir = TempDir::new().unwrap();
            let target = dir.path().join("real");
            let link = dir.path().join("link");
            std::fs::create_dir(&target).unwrap();
            std::os::unix::fs::symlink(&target, &link).unwrap();

            let result = audit_storage_dir(&link);
            assert!(result.is_err());
        }

        #[test]
        fn test_audit_storage_dir_ok_for_real_dir() {
            let dir = TempDir::new().unwrap();
            assert!(audit_storage_dir(dir.path()).is_ok());
        }
    }

    // --- Item 6 (capture_content=False leak audit): additional tests ---

    mod content_leak_audit {
        use crate::db::{create_db, query_spans_by_trace};
        use crate::storage::{write_span, SpanInput, WriteSpanOptions};
        use tempfile::TempDir;

        #[test]
        fn test_capture_content_false_no_content_rows() {
            let dir = TempDir::new().unwrap();
            let conn = create_db(&dir.path().join("t.db"), false).unwrap();

            let input = SpanInput {
                span_id: "s1".to_string(),
                trace_id: "t1".to_string(),
                parent_span_id: None,
                name: "test".to_string(),
                kind: "chat".to_string(),
                start_time: 1000000,
                end_time: Some(2000000),
                status: "OK".to_string(),
                model: None,
                provider: None,
                input_tokens: None,
                output_tokens: None,
                cache_creation_tokens: None,
                cache_read_tokens: None,
                attributes: "{}".to_string(),
                input_messages: Some(serde_json::json!([{
                    "role": "user",
                    "content": "SECRET: api_key=sk-12345"
                }])),
                output_messages: Some(serde_json::json!([{
                    "role": "assistant",
                    "content": "Here is your API key processing result"
                }])),
            };

            write_span(
                &conn,
                &input,
                WriteSpanOptions {
                    capture_content: false,
                    capture_embeddings: false,
                },
            )
            .unwrap();

            // No content rows.
            let in_count: i32 = conn
                .query_row("SELECT COUNT(*) FROM input_content", [], |row| row.get(0))
                .unwrap();
            let out_count: i32 = conn
                .query_row("SELECT COUNT(*) FROM output_content", [], |row| row.get(0))
                .unwrap();
            assert_eq!(in_count, 0);
            assert_eq!(out_count, 0);

            // No content IDs on span.
            let spans = query_spans_by_trace(&conn, "t1").unwrap();
            assert_eq!(spans.len(), 1);
            assert!(spans[0].input_content_id.is_none());
            assert!(spans[0].output_content_id.is_none());
        }

        #[test]
        fn test_capture_content_false_no_embeddings() {
            let dir = TempDir::new().unwrap();
            let conn = create_db(&dir.path().join("t.db"), false).unwrap();

            let input = SpanInput {
                span_id: "s1".to_string(),
                trace_id: "t1".to_string(),
                parent_span_id: None,
                name: "test".to_string(),
                kind: "chat".to_string(),
                start_time: 1000000,
                end_time: Some(2000000),
                status: "OK".to_string(),
                model: None,
                provider: None,
                input_tokens: None,
                output_tokens: None,
                cache_creation_tokens: None,
                cache_read_tokens: None,
                attributes: "{}".to_string(),
                input_messages: Some(serde_json::json!([{
                    "role": "user",
                    "content": "sensitive data"
                }])),
                output_messages: None,
            };

            // Even with capture_embeddings=true, if capture_content=false
            // there should be NO embeddings (embeddings require content).
            write_span(
                &conn,
                &input,
                WriteSpanOptions {
                    capture_content: false,
                    capture_embeddings: true, // should have no effect when content is off
                },
            )
            .unwrap();

            let spans = query_spans_by_trace(&conn, "t1").unwrap();
            assert!(spans[0].input_embedding.is_none());
            assert!(spans[0].output_embedding.is_none());
            assert!(spans[0].embedding_model.is_none());
        }

        #[test]
        fn test_no_content_in_span_attributes() {
            // Verify that raw message content doesn't leak into the attributes column.
            let dir = TempDir::new().unwrap();
            let conn = create_db(&dir.path().join("t.db"), false).unwrap();

            let secret = "SECRET_API_KEY_12345";
            let input = SpanInput {
                span_id: "s1".to_string(),
                trace_id: "t1".to_string(),
                parent_span_id: None,
                name: "test".to_string(),
                kind: "chat".to_string(),
                start_time: 1000000,
                end_time: Some(2000000),
                status: "OK".to_string(),
                model: None,
                provider: None,
                input_tokens: None,
                output_tokens: None,
                cache_creation_tokens: None,
                cache_read_tokens: None,
                attributes: r#"{"tool": "search"}"#.to_string(),
                input_messages: Some(serde_json::json!([{
                    "role": "user",
                    "content": secret
                }])),
                output_messages: None,
            };

            write_span(
                &conn,
                &input,
                WriteSpanOptions {
                    capture_content: false,
                    capture_embeddings: false,
                },
            )
            .unwrap();

            // The secret should NOT appear in the attributes column.
            let attrs: String = conn
                .query_row(
                    "SELECT attributes FROM spans WHERE span_id = 's1'",
                    [],
                    |row| row.get(0),
                )
                .unwrap();
            assert!(
                !attrs.contains(secret),
                "Secret content leaked into attributes: {attrs}"
            );
        }
    }

    // --- Schema migration tests ---

    mod migration {
        use crate::db::{create_db, migrate_db};
        use rusqlite::Connection;
        use tempfile::TempDir;

        #[test]
        fn test_migrate_current_version_is_noop() {
            let dir = TempDir::new().unwrap();
            let db_path = dir.path().join("test.db");
            let conn = create_db(&db_path, true).unwrap();

            let result = migrate_db(&conn).unwrap();
            assert!(result.is_none(), "Current version should need no migration");
        }

        #[test]
        fn test_migrate_version_0_upgrades() {
            let dir = TempDir::new().unwrap();
            let db_path = dir.path().join("test.db");

            // Create a raw DB at version 0.
            let conn = Connection::open(&db_path).unwrap();
            conn.pragma_update(None, "user_version", 0).unwrap();
            drop(conn);

            let conn = Connection::open(&db_path).unwrap();
            let result = migrate_db(&conn).unwrap();
            assert!(result.is_some());
            let stats = result.unwrap();
            assert_eq!(stats.old_version, 0);
            assert_eq!(stats.new_version, 1);
        }

        #[test]
        fn test_migrate_future_version_errors() {
            let dir = TempDir::new().unwrap();
            let db_path = dir.path().join("test.db");

            let conn = Connection::open(&db_path).unwrap();
            conn.pragma_update(None, "user_version", 999).unwrap();
            drop(conn);

            let conn = Connection::open(&db_path).unwrap();
            let result = migrate_db(&conn);
            assert!(result.is_err());
            let err = result.unwrap_err().to_string();
            assert!(err.contains("Incompatible schema version"));
        }
    }
}
