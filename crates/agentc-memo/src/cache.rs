//! `Cache` trait and the canonical SQLite-backed implementation.
//!
//! The trait exists so downstream crates (optimizer, profiler FFI) can depend
//! on the API without pulling in rusqlite. M2 implements only the exact-hash
//! path: `lookup` queries by `cache_key_hash`, `insert` writes one row, and
//! `invalidate`/`stats` operate on `memoization_cache` directly.
//!
//! LSH candidate retrieval (M3) plugs into `SqliteCache::lookup` after the
//! exact-hash branch misses.

use anyhow::{Context, Result};
use rusqlite::{params, Connection, OptionalExtension};

use crate::key::{cache_key_hash, CacheKey, InvalidationPattern};
use crate::schema::ensure_schema;

/// Value stored alongside a cache entry. `output_content_id` points into the
/// profiler's shared `output_content` table; the cache never stores output
/// bytes directly.
#[derive(Debug, Clone, PartialEq)]
pub struct CacheValue {
    pub output_content_id: String,
    pub input_tokens: u32,
    pub output_tokens: u32,
    pub recorded_cost_usd: f32,
}

/// Source of a cache hit. M2 always returns `Exact`; `Lsh` is wired in M3.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum CacheSource {
    Exact,
    Lsh { similarity: f32 },
}

/// Successful cache lookup.
#[derive(Debug, Clone)]
pub struct CacheHit {
    pub value: CacheValue,
    pub source: CacheSource,
    pub age_micros: i64,
}

/// Aggregated statistics returned by `Cache::stats`.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct CacheStats {
    pub entries: u64,
    pub total_hits: u64,
    pub estimated_savings_usd: f64,
    pub bytes_on_disk: u64,
}

/// Memoization cache. Implementations must be safe to call from the writer
/// thread (insert) and the request path (lookup) concurrently.
pub trait Cache: Send + Sync {
    /// Look up `key` and return the cached value if present and unexpired.
    /// Returns `None` on miss, DB error, or expiry.
    fn lookup(&self, key: &CacheKey, now_micros: i64) -> Result<Option<CacheHit>>;

    /// Insert (or refresh) a cache entry.
    fn insert(
        &self,
        key: &CacheKey,
        value: &CacheValue,
        ttl_micros: i64,
        now_micros: i64,
    ) -> Result<()>;

    /// Drop entries matching `pattern`. Returns the number of rows deleted.
    fn invalidate(&self, pattern: &InvalidationPattern) -> Result<u64>;

    /// Aggregate cache statistics.
    fn stats(&self) -> Result<CacheStats>;
}

/// SQLite-backed cache. Holds the connection behind a `Mutex` because
/// `rusqlite::Connection` is `!Sync`; only one query is in flight at a time.
pub struct SqliteCache {
    conn: std::sync::Mutex<Connection>,
}

impl SqliteCache {
    /// Wrap an existing connection. Applies the memoization DDL idempotently
    /// so the cache is ready to use after construction.
    pub fn new(conn: Connection) -> Result<Self> {
        ensure_schema(&conn)?;
        Ok(Self {
            conn: std::sync::Mutex::new(conn),
        })
    }

    /// Borrow the inner connection for tests. Not exposed publicly; downstream
    /// callers go through the trait.
    #[cfg(test)]
    fn with_conn<R>(&self, f: impl FnOnce(&Connection) -> R) -> R {
        let guard = self.conn.lock().unwrap();
        f(&guard)
    }
}

impl Cache for SqliteCache {
    fn lookup(&self, key: &CacheKey, now_micros: i64) -> Result<Option<CacheHit>> {
        let cache_key_hex = key.cache_key_hash_hex();

        let guard = self.conn.lock().map_err(|_| anyhow::anyhow!("cache mutex poisoned"))?;

        let row = guard
            .query_row(
                "SELECT output_content_id, input_tokens, output_tokens, \
                        recorded_cost_usd, created_at, expires_at \
                 FROM memoization_cache \
                 WHERE cache_key_hash = ?1",
                params![cache_key_hex],
                |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, i64>(1)?,
                        row.get::<_, i64>(2)?,
                        row.get::<_, f64>(3)?,
                        row.get::<_, i64>(4)?,
                        row.get::<_, i64>(5)?,
                    ))
                },
            )
            .optional()
            .context("memoization_cache lookup")?;

        let Some((output_content_id, input_tokens, output_tokens, cost, created_at, expires_at)) =
            row
        else {
            return Ok(None);
        };

        if expires_at <= now_micros {
            return Ok(None);
        }

        Ok(Some(CacheHit {
            value: CacheValue {
                output_content_id,
                input_tokens: input_tokens as u32,
                output_tokens: output_tokens as u32,
                recorded_cost_usd: cost as f32,
            },
            source: CacheSource::Exact,
            age_micros: now_micros - created_at,
        }))
    }

    fn insert(
        &self,
        key: &CacheKey,
        value: &CacheValue,
        ttl_micros: i64,
        now_micros: i64,
    ) -> Result<()> {
        let cache_key_hex = hex_of(&cache_key_hash(&key.prompt_hash, &key.model, &key.parameters_hash));
        let expires_at = now_micros.saturating_add(ttl_micros);

        let guard = self.conn.lock().map_err(|_| anyhow::anyhow!("cache mutex poisoned"))?;
        guard
            .execute(
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
                params![
                    cache_key_hex,
                    key.prompt_hash_hex(),
                    key.model,
                    key.parameters_hash_hex(),
                    value.output_content_id,
                    value.input_tokens as i64,
                    value.output_tokens as i64,
                    value.recorded_cost_usd as f64,
                    now_micros,
                    expires_at,
                    now_micros,
                    key.call_site_id,
                ],
            )
            .context("memoization_cache insert")?;
        Ok(())
    }

    fn invalidate(&self, pattern: &InvalidationPattern) -> Result<u64> {
        let guard = self.conn.lock().map_err(|_| anyhow::anyhow!("cache mutex poisoned"))?;
        let rows = match pattern {
            InvalidationPattern::CallSiteGlob(glob) => guard
                .execute(
                    "DELETE FROM memoization_cache WHERE call_site_id GLOB ?1",
                    params![glob],
                )
                .context("memoization_cache invalidate by glob")?,
            InvalidationPattern::OlderThan { micros } => guard
                .execute(
                    "DELETE FROM memoization_cache WHERE created_at < ?1",
                    params![micros],
                )
                .context("memoization_cache invalidate older_than")?,
            InvalidationPattern::All => guard
                .execute("DELETE FROM memoization_cache", [])
                .context("memoization_cache invalidate all")?,
        };
        // Companion rows in memoization_lsh_bucket / memoization_embedding are
        // cleaned up on the next eviction sweep (M6). M2's lookup never reads
        // them, so staleness is harmless.
        Ok(rows as u64)
    }

    fn stats(&self) -> Result<CacheStats> {
        let guard = self.conn.lock().map_err(|_| anyhow::anyhow!("cache mutex poisoned"))?;
        let (entries, total_hits, savings): (i64, i64, f64) = guard
            .query_row(
                "SELECT COUNT(*), COALESCE(SUM(hit_count), 0), \
                        COALESCE(SUM(recorded_cost_usd * hit_count), 0.0) \
                 FROM memoization_cache",
                [],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .context("memoization_cache stats")?;

        let page_count: i64 = guard
            .query_row("PRAGMA page_count", [], |row| row.get(0))
            .unwrap_or(0);
        let page_size: i64 = guard
            .query_row("PRAGMA page_size", [], |row| row.get(0))
            .unwrap_or(0);

        Ok(CacheStats {
            entries: entries as u64,
            total_hits: total_hits as u64,
            estimated_savings_usd: savings,
            bytes_on_disk: (page_count * page_size).max(0) as u64,
        })
    }
}

fn hex_of(bytes: &[u8]) -> String {
    let mut out = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        out.push_str(&format!("{b:02x}"));
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use rusqlite::Connection;

    fn sample_key(tag: u8) -> CacheKey {
        CacheKey {
            prompt_hash: [tag; 32],
            model: "gpt-4o".to_string(),
            parameters_hash: [tag ^ 0xFF; 32],
            call_site_id: format!("tests:fn_{tag}"),
        }
    }

    fn sample_value() -> CacheValue {
        CacheValue {
            output_content_id: "abc123".to_string(),
            input_tokens: 100,
            output_tokens: 50,
            recorded_cost_usd: 0.0042,
        }
    }

    fn build_cache() -> SqliteCache {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE output_content (
                content_id   TEXT PRIMARY KEY,
                content_text BLOB NOT NULL,
                created_at   INTEGER NOT NULL
            );
            INSERT INTO output_content (content_id, content_text, created_at)
            VALUES ('abc123', X'00', 1);",
        )
        .unwrap();
        SqliteCache::new(conn).unwrap()
    }

    #[test]
    fn miss_on_empty_cache() {
        let cache = build_cache();
        let hit = cache.lookup(&sample_key(1), 1_000).unwrap();
        assert!(hit.is_none());
    }

    #[test]
    fn exact_hit_roundtrip() {
        let cache = build_cache();
        let key = sample_key(1);
        let value = sample_value();
        cache.insert(&key, &value, 60_000_000, 100).unwrap();

        let hit = cache.lookup(&key, 200).unwrap().expect("should hit");
        assert_eq!(hit.value, value);
        assert_eq!(hit.source, CacheSource::Exact);
        assert_eq!(hit.age_micros, 100);
    }

    #[test]
    fn expired_entry_returns_miss() {
        let cache = build_cache();
        let key = sample_key(1);
        cache.insert(&key, &sample_value(), 10, 100).unwrap();
        // now > 110, so expired.
        let hit = cache.lookup(&key, 200).unwrap();
        assert!(hit.is_none());
    }

    #[test]
    fn insert_on_conflict_refreshes_ttl() {
        let cache = build_cache();
        let key = sample_key(1);
        cache.insert(&key, &sample_value(), 10, 100).unwrap();
        // Refresh after expiry with a longer TTL.
        cache.insert(&key, &sample_value(), 10_000, 200).unwrap();
        let hit = cache.lookup(&key, 250).unwrap();
        assert!(hit.is_some());
    }

    #[test]
    fn different_models_are_distinct() {
        let cache = build_cache();
        let mut k1 = sample_key(1);
        let mut k2 = sample_key(1);
        k2.model = "claude-sonnet-4".to_string();

        cache.insert(&k1, &sample_value(), 10_000, 100).unwrap();
        // k2 should miss since model differs.
        assert!(cache.lookup(&k2, 200).unwrap().is_none());

        k1.model = "claude-sonnet-4".to_string();
        assert!(cache.lookup(&k1, 200).unwrap().is_none());
    }

    #[test]
    fn invalidate_glob_matches_call_sites() {
        let cache = build_cache();
        let mut k1 = sample_key(1);
        k1.call_site_id = "app.router:*".to_string();
        let mut k2 = sample_key(2);
        k2.call_site_id = "app.summarizer:top".to_string();

        cache.insert(&k1, &sample_value(), 10_000, 10).unwrap();
        cache.insert(&k2, &sample_value(), 10_000, 10).unwrap();

        let removed = cache
            .invalidate(&InvalidationPattern::CallSiteGlob("app.router:*".into()))
            .unwrap();
        assert_eq!(removed, 1);
        assert!(cache.lookup(&k1, 20).unwrap().is_none());
        assert!(cache.lookup(&k2, 20).unwrap().is_some());
    }

    #[test]
    fn invalidate_all_wipes_cache() {
        let cache = build_cache();
        for t in 0u8..5 {
            cache.insert(&sample_key(t), &sample_value(), 10_000, 10).unwrap();
        }
        let removed = cache.invalidate(&InvalidationPattern::All).unwrap();
        assert_eq!(removed, 5);
        assert_eq!(cache.stats().unwrap().entries, 0);
    }

    #[test]
    fn invalidate_older_than_drops_old_entries() {
        let cache = build_cache();
        cache.insert(&sample_key(1), &sample_value(), 10_000, 10).unwrap();
        cache.insert(&sample_key(2), &sample_value(), 10_000, 10_000).unwrap();

        let removed = cache
            .invalidate(&InvalidationPattern::OlderThan { micros: 5_000 })
            .unwrap();
        assert_eq!(removed, 1);
        assert_eq!(cache.stats().unwrap().entries, 1);
    }

    #[test]
    fn stats_reports_entries_and_disk_usage() {
        let cache = build_cache();
        cache.insert(&sample_key(1), &sample_value(), 10_000, 10).unwrap();
        let stats = cache.stats().unwrap();
        assert_eq!(stats.entries, 1);
        assert!(stats.bytes_on_disk > 0);
    }

    #[test]
    fn hit_count_default_is_zero() {
        let cache = build_cache();
        let key = sample_key(7);
        cache.insert(&key, &sample_value(), 10_000, 10).unwrap();
        let count: i64 = cache.with_conn(|c| {
            c.query_row(
                "SELECT hit_count FROM memoization_cache WHERE cache_key_hash = ?1",
                params![key.cache_key_hash_hex()],
                |row| row.get(0),
            )
            .unwrap()
        });
        assert_eq!(count, 0);
    }
}
