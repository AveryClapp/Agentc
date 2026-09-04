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
use crate::exploration::ExplorationController;
use crate::model_catalog::{default_model_catalog, ModelCatalog};
use crate::plan_guard::{PlanGuard, PLAN_DISABLE_COOLDOWN_US, PLAN_EXPOSURE_WINDOW_US};
use crate::plan_profile::PlanProfiles;
use crate::planner::{Optimizer, RewriteRule};
use crate::rules::cache_hit::CacheKeyBuilder;
use crate::rules::{
    CacheHitRule, ContextCompressRule, DeadOutputTruncationRule, ModelDowngradeRule,
    OutputBudgetRule, ParallelBranchRule, PromptDedupRule, StateDropRule, StructuredTruncationRule,
};
use crate::schema::{ensure_audit_schema, ensure_cost_model_schema};

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

/// Bundle returned from [`build_optimizer`]. The FFI layer transfers the audit
/// connection to its persistence worker so no call re-opens the database.
pub struct Wired {
    pub optimizer: Arc<Optimizer>,
    pub cost_model: Arc<CostModel>,
    pub plan_profiles: Arc<PlanProfiles>,
    pub plan_guard: Arc<PlanGuard>,
    pub exploration_controller: ExplorationController,
    pub model_catalog: Arc<ModelCatalog>,
    pub budget: Arc<Budget>,
    /// Connection to `optimizer_audit.db`. The FFI layer moves it to the sole
    /// background writer.
    pub audit_conn: Connection,
}

/// Enable WAL + relaxed sync on a writable SQLite connection. SQLite's default
/// (journal_mode=DELETE, synchronous=FULL) fsyncs a rollback journal on every
/// write; WAL + NORMAL removes that per-write fsync while remaining crash-safe.
fn set_write_pragmas(conn: &Connection) -> Result<()> {
    conn.execute_batch("PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;")
        .context("set WAL pragmas")?;
    Ok(())
}

/// Construct a fully-wired optimizer rooted at `storage_dir`.
///
/// Side effects:
/// - Creates `cost_model.db` and `optimizer_audit.db` if missing.
/// - Hydrates the in-memory cost model from `call_site_profile`.
/// - Hydrates exact execution-plan outcome and paired-divergence windows.
/// - Hydrates divergence/streak state and the disable cache from their tables.
///
/// The memoization cache shares `traces.db` with the profiler — that's
/// where `@memoize` already writes — so a CacheHit served by the rule and
/// a CacheHit served by `@memoize` look identical to a downstream reader.
pub fn build_optimizer(storage_dir: &Path, config: OptimizerConfig) -> Result<Wired> {
    config.validate().context("validate optimizer configuration")?;
    let exploration_controller = ExplorationController::with_policy(config.exploration_policy())
        .context("validate exploration configuration")?;
    std::fs::create_dir_all(storage_dir)
        .with_context(|| format!("create storage dir {:?}", storage_dir))?;

    let cost_path = storage_dir.join("cost_model.db");
    let mut cost_conn =
        Connection::open(&cost_path).with_context(|| format!("open {:?}", cost_path))?;
    set_write_pragmas(&cost_conn).context("set cost_model pragmas")?;
    ensure_cost_model_schema(&cost_conn).context("ensure cost_model schema")?;

    let cost_model = Arc::new(CostModel::with_window(config.cost_model_window));
    let _ = cost_model
        .warm_from_db(&cost_conn)
        .context("warm cost_model")?;
    let _ = cost_model
        .flush_dirty(&mut cost_conn)
        .context("persist resized cost-model windows")?;

    let plan_profiles = Arc::new(PlanProfiles::with_window(config.plan_profile_window));
    let _ = plan_profiles
        .warm_from_db(&cost_conn)
        .context("warm execution-plan profiles")?;
    let _ = plan_profiles
        .flush_dirty(&mut cost_conn)
        .context("persist resized execution-plan windows")?;

    let plan_guard = Arc::new(
        PlanGuard::with_limits(
            config.divergence_exposure_budget,
            PLAN_EXPOSURE_WINDOW_US,
            PLAN_DISABLE_COOLDOWN_US,
        )
        .context("validate complete-plan guard configuration")?,
    );
    let _ = plan_guard
        .warm_from_db(&cost_conn)
        .context("warm execution-plan guard")?;
    let _ = plan_guard
        .flush_dirty(&mut cost_conn)
        .context("persist normalized execution-plan guard")?;

    let budget = Arc::new(Budget::with_window(config.divergence_window));
    let _ = budget.warm_from_db(&cost_conn).context("warm budget")?;
    let _ = budget
        .warm_divergence_from_db(&cost_conn)
        .context("warm divergence state")?;
    let _ = budget
        .flush_divergence(&mut cost_conn)
        .context("persist resized divergence windows")?;

    let model_catalog = Arc::new(default_model_catalog().context("build model catalog")?);

    let audit_path = storage_dir.join("optimizer_audit.db");
    let audit_conn =
        Connection::open(&audit_path).with_context(|| format!("open {:?}", audit_path))?;
    // The audit DB receives one row per accepted plan audit. Without WAL, SQLite runs
    // journal_mode=DELETE + synchronous=FULL and fsyncs a rollback journal on
    // EVERY transaction. WAL + NORMAL keeps background batches cheap while
    // retaining SQLite's process-crash guarantees for committed transactions.
    set_write_pragmas(&audit_conn).context("set audit pragmas")?;
    ensure_audit_schema(&audit_conn).context("ensure audit schema")?;

    // CacheHit reads from the same SQLite file as the profiler's spans and
    // `@memoize`. Open a second connection (read-mostly here; the profiler and
    // `@memoize` own the writes and the file's WAL mode).
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

    // `AGENTC_ENABLED_RULES`: comma-separated whitelist of rule names.
    // When set, only the named rules are registered. Unset = all rules.
    // Example: AGENTC_ENABLED_RULES=ContextCompress,OutputBudget
    let enabled: Option<std::collections::HashSet<String>> =
        std::env::var("AGENTC_ENABLED_RULES").ok().map(|v| {
            v.split(',')
                .map(|s| s.trim().to_string())
                .filter(|s| !s.is_empty())
                .collect()
        });
    let rule_enabled =
        |name: &str| -> bool { enabled.as_ref().is_none_or(|set| set.contains(name)) };

    let mut rules: Vec<Box<dyn RewriteRule>> = Vec::with_capacity(5);
    if rule_enabled("CacheHit") {
        if let Some(cache) = cache {
            let key_builder: Arc<dyn CacheKeyBuilder> = Arc::new(CanonicalKeyBuilder {
                provider: provider_hint(),
            });
            rules.push(Box::new(CacheHitRule::new(cache, key_builder)));
        }
    }
    if rule_enabled("ContextCompress") {
        rules.push(Box::new(ContextCompressRule::default()));
    }
    if rule_enabled("PromptDedup") {
        rules.push(Box::new(PromptDedupRule::default()));
    }
    if rule_enabled("ParallelBranch") {
        rules.push(Box::new(ParallelBranchRule::default()));
    }
    if rule_enabled("ModelDowngrade") {
        rules.push(Box::new(ModelDowngradeRule::from_catalog(
            model_catalog.clone(),
            budget.clone(),
        )));
    }
    if rule_enabled("StateDrop") {
        rules.push(Box::new(StateDropRule::default()));
    }
    if rule_enabled("OutputBudget") {
        rules.push(Box::new(OutputBudgetRule::default()));
    }
    if rule_enabled("StructuredTruncation") {
        rules.push(Box::new(StructuredTruncationRule::default()));
    }
    if rule_enabled("DeadOutputTruncation") {
        rules.push(Box::new(DeadOutputTruncationRule::default()));
    }

    let optimizer = Arc::new(Optimizer::with_budget(
        cost_model.clone(),
        rules,
        config,
        budget.clone(),
    ));

    Ok(Wired {
        optimizer,
        cost_model,
        plan_profiles,
        plan_guard,
        exploration_controller,
        model_catalog,
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cost_model::CostModelUpdate;

    #[test]
    fn writable_dbs_are_wal_mode() {
        // Regression (bd-gzm): the audit DB is written per plan call; without
        // WAL it fsyncs a rollback journal every time. build_optimizer must
        // put the writable DBs into WAL mode.
        let dir = tempfile::TempDir::new().unwrap();
        let _wired = build_optimizer(dir.path(), OptimizerConfig::default()).unwrap();

        for name in ["cost_model.db", "optimizer_audit.db"] {
            let conn = Connection::open(dir.path().join(name)).unwrap();
            let mode: String = conn
                .query_row("PRAGMA journal_mode", [], |r| r.get(0))
                .unwrap();
            assert_eq!(mode.to_lowercase(), "wal", "{name} must be WAL mode");
        }
    }

    #[test]
    fn one_config_drives_guard_and_exploration_limits() {
        let dir = tempfile::TempDir::new().unwrap();
        let config = OptimizerConfig {
            min_plan_evidence: 7,
            plan_profile_window: 10,
            exploration_calls_per_site_24h: 9,
            max_concurrent_counterfactuals: 2,
            divergence_exposure_budget: 0.25,
            evaluation_task_damage_budget: 2.0,
            ..OptimizerConfig::default()
        };
        let wired = build_optimizer(dir.path(), config).unwrap();
        assert_eq!(wired.plan_guard.exposure_budget(), 0.25);
        let policy = wired.exploration_controller.policy();
        assert_eq!(policy.evidence_target, 7);
        assert_eq!(policy.max_calls_per_site, 9);
        assert_eq!(policy.max_concurrent_per_site, 2);
        assert_eq!(policy.divergence_exposure_budget, 0.25);
        assert_eq!(policy.task_damage_budget, Some(2.0));
    }

    #[test]
    fn configured_window_controls_restart_hydration_and_persistence() {
        let dir = tempfile::TempDir::new().unwrap();
        let initial_config = OptimizerConfig {
            cost_model_window: 5,
            ..OptimizerConfig::default()
        };
        let initial = build_optimizer(dir.path(), initial_config).unwrap();
        for output_tokens in 1..=5 {
            initial.cost_model.observe(CostModelUpdate {
                call_site_id: "configured.window".to_string(),
                input_tokens: 10,
                output_tokens,
                latency_ms: 1.0,
                cost_usd: 0.01,
                output_is_structured: false,
                output_is_short: true,
                now_us: Some(output_tokens as i64),
            });
        }
        let mut connection = Connection::open(dir.path().join("cost_model.db")).unwrap();
        initial.cost_model.flush_dirty(&mut connection).unwrap();
        drop(initial);

        let resized_config = OptimizerConfig {
            cost_model_window: 3,
            ..OptimizerConfig::default()
        };
        let resized = build_optimizer(dir.path(), resized_config).unwrap();
        let profile = resized.cost_model.get("configured.window").unwrap();
        assert_eq!(profile.n_observations, 5);
        assert_eq!(profile.window_observations, 3);
        assert_eq!(profile.output_tokens.mean, 4.0);
        let retained: i64 = connection
            .query_row(
                "SELECT COUNT(*) FROM call_site_observation \
                 WHERE call_site_id = 'configured.window'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(retained, 3);
    }

    #[test]
    fn build_optimizer_hydrates_divergence_and_breach_streak() {
        let dir = tempfile::TempDir::new().unwrap();
        let initial = build_optimizer(dir.path(), OptimizerConfig::default()).unwrap();
        for expected in 1..=4 {
            assert_eq!(
                initial
                    .budget
                    .record_sample("restart.site", "OutputBudget", 0.5, 0.1, expected),
                crate::budget::SampleOutcome::Breached {
                    consecutive: expected as u32,
                }
            );
        }
        let mut connection = Connection::open(dir.path().join("cost_model.db")).unwrap();
        initial.budget.flush_divergence(&mut connection).unwrap();
        drop(initial);

        let restarted = build_optimizer(dir.path(), OptimizerConfig::default()).unwrap();
        let entry = restarted
            .budget
            .get_entry("restart.site", "OutputBudget")
            .unwrap();
        assert_eq!(entry.stats.n, 4);
        assert_eq!(entry.n_samples, 4);
        assert_eq!(entry.consecutive_breaches, 4);
        assert_eq!(
            restarted
                .budget
                .record_sample("restart.site", "OutputBudget", 0.5, 0.1, 5),
            crate::budget::SampleOutcome::Disable {
                disabled_at_us: 5,
                reenable_at_us: 5 + crate::budget::COOLDOWN_US,
            }
        );
    }

    #[test]
    fn configured_divergence_window_controls_restart_hydration_and_persistence() {
        let dir = tempfile::TempDir::new().unwrap();
        let initial_config = OptimizerConfig {
            divergence_window: 5,
            ..OptimizerConfig::default()
        };
        let initial = build_optimizer(dir.path(), initial_config).unwrap();
        for (now, divergence) in [0.1, 0.2, 0.3, 0.4, 0.5].into_iter().enumerate() {
            initial.budget.record_sample(
                "configured.divergence.window",
                "OutputBudget",
                divergence,
                1.0,
                now as i64,
            );
        }
        let mut connection = Connection::open(dir.path().join("cost_model.db")).unwrap();
        initial.budget.flush_divergence(&mut connection).unwrap();
        drop(initial);

        let resized_config = OptimizerConfig {
            divergence_window: 3,
            ..OptimizerConfig::default()
        };
        let resized = build_optimizer(dir.path(), resized_config).unwrap();
        let entry = resized
            .budget
            .get_entry("configured.divergence.window", "OutputBudget")
            .unwrap();
        assert_eq!(entry.n_samples, 5);
        assert_eq!(entry.stats.n, 3);
        assert!((entry.stats.mean - 0.4).abs() < 1e-7);
        let retained: i64 = connection
            .query_row(
                "SELECT COUNT(*) FROM rule_divergence_observation \
                 WHERE call_site_id = 'configured.divergence.window' \
                   AND rule = 'OutputBudget'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(retained, 3);
    }
}
