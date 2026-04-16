//! Agentc semantic memoization cache.
//!
//! M2 ships the exact-hash path: schema, `CacheKey` derivation, and a
//! `SqliteCache` that serves primary-key hits and records inserts. LSH fallback
//! (M3), prompt canonicalization (M4), and the `@memoize` decorator (M5) build
//! on top of what lives here.
//!
//! The `Cache` trait is the stable surface; downstream crates (optimizer,
//! profiler FFI) depend on the trait, not on `SqliteCache`.

pub mod cache;
pub mod canonical;
pub mod eviction;
pub mod ffi;
pub mod key;
pub mod lsh;
pub mod schema;

pub use cache::{Cache, CacheHit, CacheSource, CacheStats, CacheValue, SqliteCache};
pub use key::{cache_key_hash, CacheKey, InvalidationPattern, HASH_LEN};
pub use lsh::DEFAULT_SIMILARITY_THRESHOLD;
pub use schema::ensure_schema;
