//! Cache key derivation.
//!
//! A cache entry is keyed on `(prompt_hash, model, parameters_hash)`. The
//! composite `cache_key_hash` is a SHA-256 of the concatenation, encoded as a
//! 64-char lowercase hex string to match the profiler's `content_id`
//! convention.

use sha2::{Digest, Sha256};

/// Length of a SHA-256 digest in bytes.
pub const HASH_LEN: usize = 32;

/// Fully-resolved cache key.
///
/// `prompt_hash` and `parameters_hash` arrive from the canonicalizer (M4);
/// M2 stores and compares them as hex strings to match the rest of the
/// SQLite schema.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct CacheKey {
    pub prompt_hash: [u8; HASH_LEN],
    pub model: String,
    pub parameters_hash: [u8; HASH_LEN],
    pub call_site_id: String,
}

impl CacheKey {
    /// Hex form of the composite cache key — primary key in `memoization_cache`.
    pub fn cache_key_hash_hex(&self) -> String {
        hex_of(&cache_key_hash(&self.prompt_hash, &self.model, &self.parameters_hash))
    }

    /// Hex form of `prompt_hash`.
    pub fn prompt_hash_hex(&self) -> String {
        hex_of(&self.prompt_hash)
    }

    /// Hex form of `parameters_hash`.
    pub fn parameters_hash_hex(&self) -> String {
        hex_of(&self.parameters_hash)
    }
}

/// Raw cache-key composition: `SHA-256(prompt_hash || model || parameters_hash)`.
pub fn cache_key_hash(
    prompt_hash: &[u8; HASH_LEN],
    model: &str,
    parameters_hash: &[u8; HASH_LEN],
) -> [u8; HASH_LEN] {
    let mut h = Sha256::new();
    h.update(prompt_hash);
    h.update(model.as_bytes());
    h.update(parameters_hash);
    h.finalize().into()
}

/// Invalidation filter passed to `Cache::invalidate`.
#[derive(Debug, Clone)]
pub enum InvalidationPattern {
    /// SQL `GLOB` pattern matched against `call_site_id`.
    CallSiteGlob(String),
    /// Drop entries older than the given microsecond timestamp.
    OlderThan { micros: i64 },
    /// Wipe the cache.
    All,
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

    #[test]
    fn cache_key_hash_is_deterministic() {
        let p = [1u8; 32];
        let q = [2u8; 32];
        let a = cache_key_hash(&p, "gpt-4o", &q);
        let b = cache_key_hash(&p, "gpt-4o", &q);
        assert_eq!(a, b);
    }

    #[test]
    fn different_models_produce_different_keys() {
        let p = [1u8; 32];
        let q = [2u8; 32];
        assert_ne!(
            cache_key_hash(&p, "gpt-4o", &q),
            cache_key_hash(&p, "claude-sonnet-4", &q)
        );
    }

    #[test]
    fn different_parameter_hashes_produce_different_keys() {
        let p = [1u8; 32];
        let q1 = [2u8; 32];
        let q2 = [3u8; 32];
        assert_ne!(
            cache_key_hash(&p, "gpt-4o", &q1),
            cache_key_hash(&p, "gpt-4o", &q2)
        );
    }

    #[test]
    fn cache_key_hex_is_64_chars_lowercase() {
        let key = CacheKey {
            prompt_hash: [0xABu8; 32],
            model: "gpt-4o".into(),
            parameters_hash: [0xCDu8; 32],
            call_site_id: "tests:fn".into(),
        };
        let hex = key.cache_key_hash_hex();
        assert_eq!(hex.len(), 64);
        assert!(hex.chars().all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase()));
    }

    #[test]
    fn prompt_hash_hex_roundtrips() {
        let mut prompt_hash = [0u8; 32];
        prompt_hash[..8].copy_from_slice(&[0x01, 0x23, 0x45, 0x67, 0x89, 0xAB, 0xCD, 0xEF]);
        let key = CacheKey {
            prompt_hash,
            model: "m".into(),
            parameters_hash: [0u8; 32],
            call_site_id: "c".into(),
        };
        assert!(key.prompt_hash_hex().starts_with("0123456789abcdef"));
    }
}
