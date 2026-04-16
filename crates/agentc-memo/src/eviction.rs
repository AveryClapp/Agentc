//! Eviction primitives for the memoization cache.
//!
//! Three triggers share the same writer-thread cadence so they never race
//! with an insert on the same connection:
//!
//! - [`ttl_sweep`] deletes every row whose `expires_at` has passed.
//! - [`lru_evict`] drops the least-recently-hit 5 % of rows when `COUNT(*)`
//!   exceeds `max_entries`.
//! - [`maybe_vacuum`] runs `VACUUM` when the caller reports that more than
//!   64 MB has been reclaimed since the last compaction.
//!
//! The functions are pure Rust (no PyO3) so they unit-test cleanly and
//! compose with the FFI wrapper in [`crate::ffi`].

use rusqlite::Connection;

/// Percentage of rows evicted per LRU pass (5 %).
pub const LRU_EVICT_FRACTION_BP: u64 = 500;

/// Threshold for running a VACUUM: 64 MB reclaimed.
pub const VACUUM_RECLAIM_THRESHOLD_BYTES: u64 = 64 * 1024 * 1024;

/// Delete every row whose TTL has expired. Returns the number of rows removed.
///
/// `now_micros` is supplied by the caller so tests can pin the clock without
/// monkey-patching the system time source.
pub fn ttl_sweep(conn: &Connection, now_micros: i64) -> rusqlite::Result<u64> {
    let n = conn.execute(
        "DELETE FROM memoization_cache WHERE expires_at < ?1",
        rusqlite::params![now_micros],
    )?;
    Ok(n as u64)
}

/// Drop the least-recently-hit 5 % of rows if the table has more than
/// `max_entries` entries. No-ops when the cache is under-size.
///
/// Returns the number of rows evicted (zero when under the cap). `max_entries`
/// of `0` disables the check — the caller uses it as a "no LRU cap" sentinel.
pub fn lru_evict(conn: &Connection, max_entries: u64) -> rusqlite::Result<u64> {
    if max_entries == 0 {
        return Ok(0);
    }

    let count: u64 = conn.query_row(
        "SELECT COUNT(*) FROM memoization_cache",
        [],
        |row| row.get::<_, i64>(0).map(|n| n.max(0) as u64),
    )?;

    if count <= max_entries {
        return Ok(0);
    }

    // Evict 5 % of max_entries rounded up, minimum 1 row so we always make
    // progress. Using max_entries rather than count keeps the bite constant
    // across invocations — otherwise a big overshoot forces a big delete.
    let to_drop = (max_entries * LRU_EVICT_FRACTION_BP / 10_000).max(1);

    let n = conn.execute(
        "DELETE FROM memoization_cache
         WHERE cache_key_hash IN (
             SELECT cache_key_hash FROM memoization_cache
             ORDER BY last_hit_at ASC
             LIMIT ?1
         )",
        rusqlite::params![to_drop as i64],
    )?;

    Ok(n as u64)
}

/// Run `VACUUM` if the caller reports more than 64 MB reclaimed. Returns
/// `true` when the VACUUM ran.
///
/// VACUUM is a connection-global lock; the writer thread already owns the
/// only write connection so there is no contention to worry about inside
/// the process. Cross-process coordination (the spec's "flock gate") is
/// the merge coordinator's responsibility — this function is just the
/// byte-budget trigger.
pub fn maybe_vacuum(conn: &Connection, freed_bytes: u64) -> rusqlite::Result<bool> {
    if freed_bytes < VACUUM_RECLAIM_THRESHOLD_BYTES {
        return Ok(false);
    }
    conn.execute_batch("VACUUM")?;
    Ok(true)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ffi::{insert, now_micros};

    fn bootstrap() -> (tempfile::TempDir, Connection) {
        let dir = tempfile::TempDir::new().unwrap();
        let path = dir.path().join("evict.db");
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

    fn seed_rows(conn: &mut Connection, count: usize, ttl_s: i64) {
        for i in 0..count {
            let prompt_hash = [(i as u8).wrapping_mul(31); 32];
            let params_hash = [((i as u8) ^ 0xA5); 32];
            insert(
                conn,
                &prompt_hash,
                "m",
                &params_hash,
                "app.site",
                format!("body-{i}").as_bytes(),
                0,
                0,
                0.0,
                ttl_s,
                None,
            )
            .unwrap();
        }
    }

    #[test]
    fn ttl_sweep_removes_expired_only() {
        let (_dir, mut conn) = bootstrap();
        seed_rows(&mut conn, 4, 1);

        let count_before: i64 = conn
            .query_row("SELECT COUNT(*) FROM memoization_cache", [], |r| r.get(0))
            .unwrap();
        assert_eq!(count_before, 4);

        // Sweep with now=0 — nothing has expired yet.
        let fresh = ttl_sweep(&conn, 0).unwrap();
        assert_eq!(fresh, 0);

        // Jump forward past the TTL.
        let far_future = now_micros() + 10 * 1_000_000;
        let stale = ttl_sweep(&conn, far_future).unwrap();
        assert_eq!(stale, 4);
    }

    #[test]
    fn lru_evict_drops_five_percent_when_over_cap() {
        let (_dir, mut conn) = bootstrap();
        seed_rows(&mut conn, 100, 3600);

        // Stamp distinct last_hit_at so the "oldest" set is deterministic.
        for i in 0..100 {
            conn.execute(
                "UPDATE memoization_cache SET last_hit_at = ?1
                 WHERE rowid = (SELECT rowid FROM memoization_cache ORDER BY rowid ASC LIMIT 1 OFFSET ?2)",
                rusqlite::params![i as i64, i as i64],
            )
            .unwrap();
        }

        // max_entries = 100 → cache is at capacity, no eviction.
        assert_eq!(lru_evict(&conn, 100).unwrap(), 0);

        // max_entries = 99 → 5 % of 99 = 4.95, rounds down to 4 rows via
        // integer math; the cache retains (100 - 4) = 96 rows.
        let evicted = lru_evict(&conn, 99).unwrap();
        assert_eq!(evicted, 4);

        let remaining: i64 = conn
            .query_row("SELECT COUNT(*) FROM memoization_cache", [], |r| r.get(0))
            .unwrap();
        assert_eq!(remaining, 96);

        // The survivors should have last_hit_at >= 4 (we evicted the bottom 4).
        let min_hit: i64 = conn
            .query_row("SELECT MIN(last_hit_at) FROM memoization_cache", [], |r| r.get(0))
            .unwrap();
        assert_eq!(min_hit, 4);
    }

    #[test]
    fn lru_evict_noop_when_under_cap() {
        let (_dir, mut conn) = bootstrap();
        seed_rows(&mut conn, 10, 3600);
        assert_eq!(lru_evict(&conn, 100).unwrap(), 0);
    }

    #[test]
    fn lru_evict_zero_cap_disables_check() {
        let (_dir, mut conn) = bootstrap();
        seed_rows(&mut conn, 5, 3600);
        assert_eq!(lru_evict(&conn, 0).unwrap(), 0);
    }

    #[test]
    fn maybe_vacuum_below_threshold_is_noop() {
        let (_dir, conn) = bootstrap();
        assert!(!maybe_vacuum(&conn, VACUUM_RECLAIM_THRESHOLD_BYTES - 1).unwrap());
    }

    #[test]
    fn maybe_vacuum_at_threshold_runs() {
        let (_dir, conn) = bootstrap();
        assert!(maybe_vacuum(&conn, VACUUM_RECLAIM_THRESHOLD_BYTES).unwrap());
    }
}
