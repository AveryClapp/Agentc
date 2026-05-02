//! Production wiring helpers.
//!
//! The optimizer crate ships the rules and the planner; this module is the
//! glue that turns a storage directory into a fully-wired `Optimizer` ready
//! for the FFI. Tests construct the planner directly with mock rules — the
//! helpers here are only used at process boot (see
//! `agentc-profiler::lib::optimizer_state`).
//!
//! Failures are logged and downgraded to "empty optimizer" so a corrupted
//! `cost_model.db` never breaks the user's LLM call.

use std::path::Path;
use std::sync::Arc;

use anyhow::{Context, Result};
use rusqlite::Connection;
use serde_json::Value;
use sha2::{Digest, Sha256};

use agentc_memo::canonical::{canonicalize_parameters, canonicalize_prompt};
use agentc_memo::key::{CacheKey, HASH_LEN};
use agentc_memo::SqliteCache;

use crate::budget::Budget;
use crate::config::OptimizerConfig;
use crate::cost_model::CostModel;
use crate::dag::Call;
use crate::planner::{Optimizer, RewriteRule};
use crate::rules::cache_hit::CacheKeyBuilder;
use crate::rules::{
    CacheHitRule, ContextCompressRule, ModelDowngradeRoute, ModelDowngradeRule,
    ParallelBranchRule, StateDropRule,
};
use crate::schema::{ensure_audit_schema, ensure_cost_model_schema};

/// Default GPT-4o-mini downgrade route. Spec § Rule specifications >
/// ModelDowngrade. The price ratio is `gpt-4o-mini / gpt-4o ≈ 0.067`.
fn default_routes() -> Vec<ModelDowngradeRoute> {
    vec![
        ModelDowngradeRoute {
            from: "gpt-4o".to_string(),
            to: "gpt-4o-mini".to_string(),
            price_ratio: 0.07,
            max_output_tokens: 256,
        },
        ModelDowngradeRoute {
            from: "gpt-4-turbo".to_string(),
            to: "gpt-4o-mini".to_string(),
            price_ratio: 0.05,
            max_output_tokens: 256,
        },
    ]
}

/// Canonical-bytes-based `CacheKeyBuilder`. Hashes the call's messages and
/// parameters via the same canonicalizer used by the `@memoize` decorator,
/// so a cached response inserted by `@memoize` is reachable through the
/// optimizer's `CacheHit` rule and vice-versa.
struct CanonicalKeyBuilder {
    provider: String,
}

impl CacheKeyBuilder for CanonicalKeyBuilder {
    fn build(&self, call: &Call) -> CacheKey {
        let messages = serde_json::to_value(&call.messages).unwrap_or(Value::Null);
        let prompt_bytes = canonicalize_prompt(&messages, &self.provider);
        let prompt_hash = sha256_arr(&prompt_bytes);

        let params = serde_json::to_value(&call.parameters).unwrap_or(Value::Null);
        let params_bytes = canonicalize_parameters(&params);
        let parameters_hash = sha256_arr(&params_bytes);

        CacheKey {
            prompt_hash,
            model: call.model.clone(),
            parameters_hash,
            call_site_id: call.call_site_id.clone(),
        }
    }
}

fn sha256_arr(bytes: &[u8]) -> [u8; HASH_LEN] {
    Sha256::digest(bytes).into()
}

/// Provider hint for canonicalization. We pick one for the process based on
/// a coarse model-name probe; in practice all our reference benches use
/// OpenAI, but supporting Anthropic without an env var means cross-vendor
/// runs Just Work.
fn provider_hint() -> String {
    std::env::var("AGENTC_PROVIDER")
        .unwrap_or_else(|_| "openai".to_string())
        .to_lowercase()
}

/// Bundle returned from [`build_optimizer`]. Caller (the FFI layer) holds
/// the audit connection separately so it can write `plan_audit` rows
/// without re-opening the DB on every call.
pub struct Wired {
    pub optimizer: Arc<Optimizer>,
    pub cost_model: Arc<CostModel>,
    pub budget: Arc<Budget>,
    /// Connection to `optimizer_audit.db`. The FFI layer wraps it in a
    /// `Mutex` and uses it for synchronous `plan_audit` inserts.
    pub audit_conn: Connection,
}

/// Construct a fully-wired optimizer rooted at `storage_dir`.
///
/// Side effects:
/// - Creates `cost_model.db` and `optimizer_audit.db` if missing.
/// - Hydrates the in-memory cost model from `call_site_profile`.
/// - Hydrates the in-memory disable cache from `optimizer_disabled`.
///
/// The memoization cache shares `traces.db` with the profiler — that's
/// where `@memoize` already writes — so a CacheHit served by the rule and
/// a CacheHit served by `@memoize` look identical to a downstream reader.
pub fn build_optimizer(storage_dir: &Path, config: OptimizerConfig) -> Result<Wired> {
    std::fs::create_dir_all(storage_dir)
        .with_context(|| format!("create storage dir {:?}", storage_dir))?;

    let cost_path = storage_dir.join("cost_model.db");
    let cost_conn = Connection::open(&cost_path)
        .with_context(|| format!("open {:?}", cost_path))?;
    ensure_cost_model_schema(&cost_conn).context("ensure cost_model schema")?;

    let cost_model = Arc::new(CostModel::new());
    let _ = cost_model.warm_from_db(&cost_conn).context("warm cost_model")?;

    let budget = Arc::new(Budget::new());
    let _ = budget.warm_from_db(&cost_conn).context("warm budget")?;

    let audit_path = storage_dir.join("optimizer_audit.db");
    let audit_conn = Connection::open(&audit_path)
        .with_context(|| format!("open {:?}", audit_path))?;
    ensure_audit_schema(&audit_conn).context("ensure audit schema")?;

    // CacheHit reads from the same SQLite file as the profiler's spans
    // and `@memoize`. Open a second connection (read-mostly here, writes
    // happen elsewhere) — SQLite's WAL mode handles concurrent access.
    let traces_path = storage_dir.join("traces.db");
    let cache: Option<Arc<dyn agentc_memo::Cache>> = match Connection::open(&traces_path) {
        Ok(c) => match SqliteCache::new(c) {
            Ok(sc) => Some(Arc::new(sc)),
            Err(e) => {
                tracing_warn(&format!("optimizer: SqliteCache init failed: {e}"));
                None
            }
        },
        Err(e) => {
            tracing_warn(&format!("optimizer: open traces.db failed: {e}"));
            None
        }
    };

    let mut rules: Vec<Box<dyn RewriteRule>> = Vec::with_capacity(5);
    if let Some(cache) = cache {
        let key_builder: Arc<dyn CacheKeyBuilder> = Arc::new(CanonicalKeyBuilder {
            provider: provider_hint(),
        });
        rules.push(Box::new(CacheHitRule::new(cache, key_builder)));
    }
    rules.push(Box::new(ContextCompressRule::default()));
    rules.push(Box::new(ParallelBranchRule::default()));
    rules.push(Box::new(ModelDowngradeRule::new(
        default_routes(),
        budget.clone(),
    )));
    rules.push(Box::new(StateDropRule::default()));

    let optimizer = Arc::new(Optimizer::with_budget(
        cost_model.clone(),
        rules,
        config,
        budget.clone(),
    ));

    Ok(Wired {
        optimizer,
        cost_model,
        budget,
        audit_conn,
    })
}

fn tracing_warn(msg: &str) {
    // We don't depend on `tracing` in this crate; eprintln is deliberately
    // cheap and survives a missing logger. Production builds run under
    // `agentc record` which captures stderr — these warnings show up in
    // the user's session log without a logger setup step.
    eprintln!("[agentc-optimizer] {msg}");
}
