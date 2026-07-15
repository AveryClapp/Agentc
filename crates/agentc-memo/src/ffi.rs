//! Pure-Rust orchestration primitives for the profiler's FFI layer.
//!
//! PyO3 bindings live in `agentc-profiler` so we can ship one cdylib. This
//! module exposes connection-in / plain-data-out helpers the profiler wraps
//! without dragging pyo3 into `agentc-memo`.
//!
//! All entry points are **fail-open**: on an internal error they log via
//! `eprintln!` and return a safe value (miss, zero rows affected, default
//! stats). A cache fault never propagates to the user's LLM call.

use std::collections::{HashMap, HashSet};
use std::path::PathBuf;
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{SystemTime, UNIX_EPOCH};

use rusqlite::Connection;
use sha2::{Digest, Sha256};

use crate::cache::{Cache, CacheHit, CacheStats, SqliteCache};
use crate::key::{CacheKey, InvalidationPattern, HASH_LEN};
use crate::lsh::write_lsh_rows;
use crate::schema::ensure_schema;

/// Current unix time in microseconds. Falls back to 0 if the system clock is
/// pre-epoch (shouldn't happen; safe value if it does).
pub fn now_micros() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_micros() as i64)
        .unwrap_or(0)
}

/// Convert a slice into a `[u8; HASH_LEN]` hash. Returns `None` if the slice
/// is the wrong length.
pub fn hash_from_bytes(bytes: &[u8]) -> Option<[u8; HASH_LEN]> {
    if bytes.len() != HASH_LEN {
        return None;
    }
    let mut out = [0u8; HASH_LEN];
    out.copy_from_slice(bytes);
    Some(out)
}

/// SHA-256 of `data`, returning a 32-byte digest.
pub fn sha256(data: &[u8]) -> [u8; HASH_LEN] {
    let mut h = Sha256::new();
    h.update(data);
    h.finalize().into()
}

/// Hex encoding of a byte slice (lowercase, no separators).
pub fn hex(bytes: &[u8]) -> String {
    let mut out = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        out.push_str(&format!("{b:02x}"));
    }
    out
}

/// FFI-safe `lookup`. Returns `None` on miss or on any error. Failures do not
/// surface — a miss is always a safe fallback.
///
/// `embedding` is the 256-dim query vector when the caller wants LSH
/// fallback; pass `None` to restrict to exact-hash lookup. `similarity`
/// overrides the cache's default threshold — `1.0` disables LSH.
pub fn lookup(
    conn: &Connection,
    prompt_hash: &[u8],
    model: &str,
    parameters_hash: &[u8],
    call_site_id: &str,
    embedding: Option<&[f32]>,
    similarity: Option<f32>,
) -> Option<CacheHit> {
    let prompt_hash = hash_from_bytes(prompt_hash)?;
    let parameters_hash = hash_from_bytes(parameters_hash)?;
    let key = CacheKey {
        prompt_hash,
        model: model.to_string(),
        parameters_hash,
        call_site_id: call_site_id.to_string(),
    };

    let cache = match shared_cache(conn) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("agentc-memo: lookup cache init failed: {e}");
            return None;
        }
    };
    // Pass the request's threshold explicitly so the shared cache is never
    // mutated (the old code called set_similarity_threshold per request).
    let threshold = similarity.unwrap_or_else(|| cache.similarity_threshold());
    match cache.lookup_with_embedding_threshold(&key, embedding, now_micros(), threshold) {
        Ok(hit) => hit,
        Err(e) => {
            eprintln!("agentc-memo: lookup failed: {e}");
            None
        }
    }
}

/// FFI-safe `insert`. Writes `output_bytes` into `output_content` (via the
/// profiler's dedup hash) and records the cache entry. All state changes
/// happen in a single transaction.
///
/// Takes ownership of the output bytes so the caller can drop its reference
/// immediately.
#[allow(clippy::too_many_arguments)]
pub fn insert(
    conn: &mut Connection,
    prompt_hash: &[u8],
    model: &str,
    parameters_hash: &[u8],
    call_site_id: &str,
    output_bytes: &[u8],
    input_tokens: u32,
    output_tokens: u32,
    recorded_cost_usd: f32,
    ttl_seconds: i64,
    embedding: Option<&[f32]>,
) -> Result<(), String> {
    let prompt_hash = hash_from_bytes(prompt_hash).ok_or_else(|| "prompt_hash wrong length".to_string())?;
    let parameters_hash =
        hash_from_bytes(parameters_hash).ok_or_else(|| "parameters_hash wrong length".to_string())?;

    let output_content_id = hex(&sha256(output_bytes));
    let now = now_micros();
    let ttl_micros = ttl_seconds.saturating_mul(1_000_000);

    ensure_schema_once(conn).map_err(|e| format!("ensure_schema: {e}"))?;

    let tx = conn.transaction().map_err(|e| format!("begin: {e}"))?;

    tx.execute(
        "INSERT OR IGNORE INTO output_content (content_id, content_text, created_at) \
         VALUES (?1, ?2, ?3)",
        rusqlite::params![output_content_id, output_bytes, now],
    )
    .map_err(|e| format!("output_content insert: {e}"))?;

    let key = CacheKey {
        prompt_hash,
        model: model.to_string(),
        parameters_hash,
        call_site_id: call_site_id.to_string(),
    };

    tx.execute(
        "INSERT INTO memoization_cache (
            cache_key_hash, prompt_hash, model, parameters_hash,
            output_content_id, input_tokens, output_tokens, recorded_cost_usd,
            created_at, expires_at, last_hit_at, hit_count, call_site_id
        ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, 0, ?12)
         ON CONFLICT(cache_key_hash) DO UPDATE SET
            expires_at = excluded.expires_at,
            recorded_cost_usd = excluded.recorded_cost_usd,
            input_tokens = excluded.input_tokens,
            output_tokens = excluded.output_tokens,
            output_content_id = excluded.output_content_id",
        rusqlite::params![
            key.cache_key_hash_hex(),
            key.prompt_hash_hex(),
            key.model,
            key.parameters_hash_hex(),
            output_content_id,
            input_tokens as i64,
            output_tokens as i64,
            recorded_cost_usd as f64,
            now,
            now.saturating_add(ttl_micros),
            now,
            key.call_site_id,
        ],
    )
    .map_err(|e| format!("memoization_cache insert: {e}"))?;

    if let Some(e) = embedding {
        write_lsh_rows(&tx, &key.cache_key_hash_hex(), e)
            .map_err(|err| format!("lsh rows: {err}"))?;
    }

    tx.commit().map_err(|e| format!("commit: {e}"))?;
    Ok(())
}

/// Combined maintenance pass: TTL sweep → LRU cap → opportunistic VACUUM.
///
/// Returns `(ttl_rows, lru_rows, vacuumed)`. Errors are fail-open: any step
/// that fails returns `0` / `false` for its slot and the remaining steps
/// still run.
pub fn maintenance(conn: &Connection, max_entries: u64) -> (u64, u64, bool) {
    if let Err(e) = ensure_schema_once(conn) {
        eprintln!("agentc-memo: maintenance schema setup failed: {e}");
        return (0, 0, false);
    }

    // Capture size before so we can estimate reclaimed bytes. `page_count *
    // page_size` is SQLite's on-disk footprint — close enough to drive the
    // VACUUM trigger.
    let size_before = on_disk_bytes(conn).unwrap_or(0);

    let ttl_rows = match crate::eviction::ttl_sweep(conn, now_micros()) {
        Ok(n) => n,
        Err(e) => {
            eprintln!("agentc-memo: ttl_sweep failed: {e}");
            0
        }
    };

    let lru_rows = match crate::eviction::lru_evict(conn, max_entries) {
        Ok(n) => n,
        Err(e) => {
            eprintln!("agentc-memo: lru_evict failed: {e}");
            0
        }
    };

    let size_after = on_disk_bytes(conn).unwrap_or(size_before);
    let freed = size_before.saturating_sub(size_after);

    let vacuumed = match crate::eviction::maybe_vacuum(conn, freed) {
        Ok(v) => v,
        Err(e) => {
            eprintln!("agentc-memo: maybe_vacuum failed: {e}");
            false
        }
    };

    (ttl_rows, lru_rows, vacuumed)
}

fn on_disk_bytes(conn: &Connection) -> rusqlite::Result<u64> {
    let page_count: i64 =
        conn.query_row("PRAGMA page_count", [], |r| r.get(0))?;
    let page_size: i64 = conn.query_row("PRAGMA page_size", [], |r| r.get(0))?;
    Ok((page_count.max(0) as u64).saturating_mul(page_size.max(0) as u64))
}

/// FFI-safe `invalidate`. Returns rows deleted, or 0 on error.
pub fn invalidate(conn: &Connection, pattern: InvalidationPattern) -> u64 {
    let cache = match shared_cache(conn) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("agentc-memo: invalidate cache init failed: {e}");
            return 0;
        }
    };
    match cache.invalidate(&pattern) {
        Ok(n) => n,
        Err(e) => {
            eprintln!("agentc-memo: invalidate failed: {e}");
            0
        }
    }
}

/// FFI-safe `stats`. Returns a default zeroed struct on error.
pub fn stats(conn: &Connection) -> CacheStats {
    let cache = match shared_cache(conn) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("agentc-memo: stats cache init failed: {e}");
            return CacheStats::default();
        }
    };
    cache.stats().unwrap_or_default()
}

// ---------------------------------------------------------------------------
// Non-owning cache wrapper.
// ---------------------------------------------------------------------------

/// Process-global pool of long-lived caches, one per DB path. The old
/// `from_shared` opened a NEW connection and re-ran the full memoization DDL
/// on EVERY lookup while holding the profiler mutex (bd-ul9). Reusing one
/// `Arc<SqliteCache>` per path removes both the connect and the DDL from the
/// hot path.
static SHARED_CACHES: OnceLock<Mutex<HashMap<PathBuf, Arc<SqliteCache>>>> = OnceLock::new();

/// Paths whose memoization schema has already been ensured this process, so
/// `insert()` / `maintenance()` skip the redundant (idempotent but not free)
/// DDL that previously ran on every FFI call.
static SCHEMA_ENSURED: OnceLock<Mutex<HashSet<PathBuf>>> = OnceLock::new();

fn conn_path(conn: &Connection) -> Option<PathBuf> {
    conn.path().map(PathBuf::from)
}

fn mark_schema_ensured(path: &std::path::Path) {
    let set = SCHEMA_ENSURED.get_or_init(|| Mutex::new(HashSet::new()));
    set.lock().unwrap_or_else(|e| e.into_inner()).insert(path.to_path_buf());
}

/// Return a long-lived shared cache for the file backing `conn`, creating it
/// (and applying the DDL) at most once per path. Errors for path-less
/// (in-memory) connections, which cannot be shared across handles.
fn shared_cache(conn: &Connection) -> anyhow::Result<Arc<SqliteCache>> {
    let path = conn_path(conn)
        .ok_or_else(|| anyhow::anyhow!("connection has no file path (in-memory?)"))?;
    let map = SHARED_CACHES.get_or_init(|| Mutex::new(HashMap::new()));
    if let Some(existing) = map.lock().unwrap_or_else(|e| e.into_inner()).get(&path).cloned() {
        return Ok(existing);
    }
    // Open a second handle to the same file (readers/writers to a WAL DB play
    // nicely). SqliteCache::new applies the DDL once.
    let fresh = Connection::open(&path)?;
    fresh.execute_batch("PRAGMA busy_timeout = 5000;")?;
    let cache = Arc::new(SqliteCache::new(fresh)?);
    mark_schema_ensured(&path);
    // Insert-or-get under the lock so a racing thread doesn't get a second cache.
    let mut guard = map.lock().unwrap_or_else(|e| e.into_inner());
    Ok(guard.entry(path).or_insert(cache).clone())
}

/// Ensure the memoization schema on `conn`, but at most once per DB path per
/// process. In-memory (path-less) connections always ensure (cheap, unkeyable).
fn ensure_schema_once(conn: &Connection) -> anyhow::Result<()> {
    let Some(path) = conn_path(conn) else {
        return ensure_schema(conn);
    };
    let set = SCHEMA_ENSURED.get_or_init(|| Mutex::new(HashSet::new()));
    if set.lock().unwrap_or_else(|e| e.into_inner()).contains(&path) {
        return Ok(());
    }
    ensure_schema(conn)?;
    set.lock().unwrap_or_else(|e| e.into_inner()).insert(path);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    fn bootstrap_db() -> (TempDir, rusqlite::Connection) {
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("test.db");
        let conn = Connection::open(&path).unwrap();
        conn.execute_batch(
            "PRAGMA journal_mode = WAL;
             CREATE TABLE output_content (
                content_id   TEXT PRIMARY KEY,
                content_text BLOB NOT NULL,
                created_at   INTEGER NOT NULL
             );",
        )
        .unwrap();
        (dir, conn)
    }

    #[test]
    fn sha256_matches_known_vector() {
        // SHA-256("") well-known output.
        let digest = sha256(b"");
        assert_eq!(
            hex(&digest),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        );
    }

    #[test]
    fn hash_from_bytes_length_check() {
        assert!(hash_from_bytes(&[0u8; 31]).is_none());
        assert!(hash_from_bytes(&[0u8; 33]).is_none());
        assert!(hash_from_bytes(&[0u8; 32]).is_some());
    }

    #[test]
    fn shared_cache_reuses_one_instance_per_path() {
        // Regression (bd-ul9): repeat FFI calls for the same DB must reuse one
        // long-lived cache (connection + ensured schema), not open a fresh
        // connection and re-run the DDL every time.
        let (_dir, conn) = bootstrap_db();
        let a = shared_cache(&conn).unwrap();
        let b = shared_cache(&conn).unwrap();
        assert!(Arc::ptr_eq(&a, &b), "same path must reuse the same cache instance");
    }

    #[test]
    fn insert_then_lookup_roundtrip() {
        let (_dir, mut conn) = bootstrap_db();
        let prompt_hash = [1u8; 32];
        let params_hash = [2u8; 32];

        insert(
            &mut conn,
            &prompt_hash,
            "gpt-4o",
            &params_hash,
            "app:call",
            b"hello",
            10,
            20,
            0.001,
            3600,
            None,
        )
        .unwrap();

        let hit = lookup(
            &conn,
            &prompt_hash,
            "gpt-4o",
            &params_hash,
            "app:call",
            None,
            None,
        );
        let hit = hit.expect("roundtrip lookup should hit");
        assert_eq!(hit.value.input_tokens, 10);
        assert_eq!(hit.value.output_tokens, 20);
        assert_eq!(hit.value.output_content_id, hex(&sha256(b"hello")));
    }

    #[test]
    fn insert_writes_output_content() {
        let (_dir, mut conn) = bootstrap_db();
        insert(
            &mut conn,
            &[1u8; 32],
            "m",
            &[2u8; 32],
            "c",
            b"bytes",
            1,
            1,
            0.0,
            1,
            None,
        )
        .unwrap();
        let n: i64 = conn
            .query_row("SELECT COUNT(*) FROM output_content", [], |r| r.get(0))
            .unwrap();
        assert_eq!(n, 1);
    }

    #[test]
    fn lookup_returns_none_for_invalid_hash_length() {
        let (_dir, conn) = bootstrap_db();
        assert!(lookup(&conn, &[0u8; 10], "m", &[0u8; 32], "c", None, None).is_none());
        assert!(lookup(&conn, &[0u8; 32], "m", &[0u8; 10], "c", None, None).is_none());
    }

    #[test]
    fn stats_empty_cache_reports_zero_entries() {
        let (_dir, conn) = bootstrap_db();
        let s = stats(&conn);
        assert_eq!(s.entries, 0);
        assert_eq!(s.total_hits, 0);
        assert_eq!(s.estimated_savings_usd, 0.0);
    }

    #[test]
    fn invalidate_all_via_ffi() {
        let (_dir, mut conn) = bootstrap_db();
        for tag in 0u8..3 {
            let prompt_hash = [tag; 32];
            let params_hash = [tag ^ 0xFF; 32];
            insert(
                &mut conn,
                &prompt_hash,
                "m",
                &params_hash,
                "c",
                b"bytes",
                1,
                1,
                0.0,
                3600,
                None,
            )
            .unwrap();
        }
        let removed = invalidate(&conn, InvalidationPattern::All);
        assert_eq!(removed, 3);
    }
}
