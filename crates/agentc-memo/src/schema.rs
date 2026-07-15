//! SQLite schema for the memoization tables.
//!
//! The memoization tables live inside `traces.db`, which `agentc-core` owns
//! (its `output_content` table is referenced by `memoization_cache`). The DDL
//! therefore lives in `agentc_core::memo_schema`; this module re-exports it so
//! `agentc-memo`'s callers keep a stable `crate::schema::ensure_schema` path.
//! Owning the DDL in one place (the crate that owns the file) removes the old
//! `agentc-core -> agentc-memo` dependency, which pointed the wrong way (bd-smg).

pub use agentc_core::memo_schema::{ensure_memoization_schema as ensure_schema, MEMOIZATION_SCHEMA};
