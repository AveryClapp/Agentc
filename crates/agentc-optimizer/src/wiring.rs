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

use agentc_memo::key::{CacheKey, HASH_LEN};
use agentc_memo::SqliteCache;

use crate::budget::Budget;
use crate::config::OptimizerConfig;
use crate::cost_model::CallSiteProfile;
use crate::cost_model::CostModel;
use crate::dag::Call;
use crate::model_catalog::{
    default_model_catalog, ModelCatalog, RequestRequirements, PROVIDER_CONFIGURATION_KEY,
    ROUTE_CONTEXT_KEY,
};
use crate::plan_guard::PlanGuard;
use crate::plan_profile::PlanProfiles;
use crate::planner::{Optimizer, Proposal, RewriteRule, RuleIdentity};
use crate::rules::cache_hit::CacheKeyBuilder;
use crate::rules::{
    CacheHitRule, ContextCompressRule, DeadOutputTruncationRule, ModelDowngradeRule,
    OutputBudgetRule, ParallelBranchRule, PromptDedupRule, StateDropRule, StructuredTruncationRule,
};
use crate::schema::{ensure_audit_schema, ensure_cost_model_schema};

pub const SDK_RESPONSE_CACHE_VERSION: &str = "optimizer-sdk-response-cache-v1";

/// Shared by production lookup and SDK seeding. Legacy function cache entries
/// retain their old namespace and cannot satisfy this request contract.
pub fn sdk_response_cache_key(call: &Call) -> Option<CacheKey> {
    let request = RequestRequirements::from_call(call)?;
    if ![
        crate::model_catalog::OPENAI_CHAT_COMPLETIONS_PROTOCOL,
        crate::model_catalog::ANTHROPIC_MESSAGES_PROTOCOL,
        crate::model_catalog::LITELLM_COMPLETION_PROTOCOL,
    ]
    .contains(&request.provider_protocol.as_str())
    {
        return None;
    }
    if request.streaming
        || request.image_input
        || call.model.is_empty()
        || call.call_site_id.is_empty()
    {
        return None;
    }
    let context = call.parameters.extra.get(ROUTE_CONTEXT_KEY)?.as_object()?;
    let configuration = context.get(PROVIDER_CONFIGURATION_KEY)?;
    let declaration = context.get("sdk_response_cache")?.as_object()?;
    if declaration.len() != 4
        || declaration.get("version")?.as_str()? != SDK_RESPONSE_CACHE_VERSION
        || !declaration.get("supported")?.as_bool()?
    {
        return None;
    }
    let digest = |name| {
        let value = declaration.get(name)?.as_str()?;
        (value.len() == 64
            && value
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)))
        .then_some(value)
    };
    let messages = digest("message_sha256")?;
    let shapes = digest("shape_sha256")?;
    let mut parameters = call.parameters.clone();
    parameters.extra = Value::Null;
    let contracts: serde_json::Map<String, Value> = [
        "structured_output_schema_version",
        "tool_settings_version",
        "tool_action_comparator_version",
        "text_action_comparator_version",
        "text_action_settings_version",
        "text_action_contract_supported",
        "frozen_request_policy",
    ]
    .into_iter()
    .filter_map(|name| {
        context
            .get(name)
            .map(|value| (name.to_string(), value.clone()))
    })
    .collect();
    let prompt = serde_json::to_vec(
        &serde_json::json!({"version":SDK_RESPONSE_CACHE_VERSION,"messages":messages}),
    )
    .ok()?;
    let settings = serde_json::to_vec(&serde_json::json!({
        "version":SDK_RESPONSE_CACHE_VERSION,
        "call_site_id":call.call_site_id,
        "provider_protocol":request.provider_protocol,"provider_namespace":request.provider_namespace,
        "parameters":parameters,"provider_configuration":configuration,"response_shape":shapes,
        "contracts":contracts,
    })).ok()?;
    Some(CacheKey {
        prompt_hash: sha256_arr(&prompt),
        model: call.model.clone(),
        parameters_hash: sha256_arr(&settings),
        call_site_id: call.call_site_id.clone(),
    })
}

struct SdkResponseKeyBuilder;

impl CacheKeyBuilder for SdkResponseKeyBuilder {
    fn supports_native_messages(&self) -> bool {
        // The SDK declares a digest of original messages. No projected DAG
        // text is used, and opaque or incomplete declarations return None.
        true
    }
    fn identity(&self) -> Option<Value> {
        Some(serde_json::json!({"implementation_version": SDK_RESPONSE_CACHE_VERSION}))
    }

    fn build(&self, call: &Call) -> Option<CacheKey> {
        sdk_response_cache_key(call)
    }
}

fn sha256_arr(bytes: &[u8]) -> [u8; HASH_LEN] {
    Sha256::digest(bytes).into()
}

/// Bundle returned from [`build_optimizer`]. Caller (the FFI layer) holds
/// the audit connection separately so it can write `plan_audit` rows
/// without re-opening the DB on every call.
pub struct Wired {
    pub optimizer: Arc<Optimizer>,
    pub cost_model: Arc<CostModel>,
    pub plan_profiles: Arc<PlanProfiles>,
    pub plan_guard: Arc<PlanGuard>,
    pub model_catalog: Arc<ModelCatalog>,
    pub budget: Arc<Budget>,
    /// Connection to `optimizer_audit.db`. The FFI layer wraps it in a
    /// `Mutex` and uses it for synchronous `plan_audit` inserts.
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
    config.validate().map_err(anyhow::Error::msg)?;
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

    let plan_guard = Arc::new(PlanGuard::with_limits(
        config.selection.divergence_exposure_budget,
        crate::plan_guard::PLAN_EXPOSURE_WINDOW_US,
        crate::plan_guard::PLAN_DISABLE_COOLDOWN_US,
    )?);
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
    // The audit DB is written once per plan call. Without WAL, SQLite runs
    // journal_mode=DELETE + synchronous=FULL and fsyncs a rollback journal on
    // EVERY write — on the user's LLM-call path (bd-gzm). WAL + NORMAL removes
    // the per-call fsync while keeping crash-safety.
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
    let rule_enabled = |name: &str| -> bool {
        config
            .rules
            .get(name)
            .and_then(|settings| settings.enabled)
            .unwrap_or(true)
    };

    let mut rules: Vec<Box<dyn RewriteRule>> = Vec::with_capacity(5);
    if rule_enabled("CacheHit") {
        if let Some(cache) = cache {
            let key_builder: Arc<dyn CacheKeyBuilder> = Arc::new(SdkResponseKeyBuilder);
            rules.push(Box::new(
                CacheHitRule::new(cache, key_builder)
                    .with_cache_policy_version("agentc-sqlite-cache-ttl-v1"),
            ));
        }
    }
    if rule_enabled("ContextCompress") {
        let settings = config
            .rules
            .get("ContextCompress")
            .cloned()
            .unwrap_or_default();
        rules.push(Box::new(ContextCompressRule::new(
            settings
                .min_prompt_bytes
                .unwrap_or(crate::rules::context_compress::DEFAULT_MIN_PROMPT_BYTES),
            settings
                .min_dead_fraction
                .unwrap_or(crate::rules::context_compress::DEFAULT_MIN_DEAD_FRACTION),
        )));
    }
    if rule_enabled("PromptDedup") {
        rules.push(Box::new(PromptDedupRule::default()));
    }
    if rule_enabled("ParallelBranch") {
        rules.push(Box::new(ParallelBranchRule::new(
            config
                .rules
                .get("ParallelBranch")
                .and_then(|settings| settings.max_fanout)
                .unwrap_or(4),
        )));
    }
    if rule_enabled("ModelDowngrade") {
        let rule = ModelDowngradeRule::from_catalog(model_catalog.clone(), budget.clone());
        rules.push(Box::new(
            if let Some(budget) = config.accuracy_budget.get("ModelDowngrade") {
                rule.with_accuracy_budget(*budget)
            } else {
                rule
            },
        ));
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

    let rules = rules
        .into_iter()
        .map(|rule| match config.accuracy_budget.get(rule.name()) {
            None => rule,
            Some(budget) => Box::new(ConfiguredBudget {
                rule,
                budget: *budget,
            }) as Box<dyn RewriteRule>,
        })
        .collect();
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
        model_catalog,
        budget,
        audit_conn,
    })
}

/// Share configuration handling while preserving each rule's implementation
/// and mutation. ModelDowngrade also receives its internal probation threshold.
struct ConfiguredBudget {
    rule: Box<dyn RewriteRule>,
    budget: f32,
}
impl RewriteRule for ConfiguredBudget {
    fn name(&self) -> &'static str {
        self.rule.name()
    }
    fn identity(&self) -> Option<RuleIdentity> {
        let mut identity = self.rule.identity()?;
        identity.parameters["accuracy_budget"] = serde_json::json!(self.budget);
        Some(identity)
    }
    fn applies(&self, call: &Call, profile: &CallSiteProfile) -> bool {
        self.rule.applies(call, profile)
    }
    fn propose(&self, call: &Call, profile: &CallSiteProfile) -> Option<Proposal> {
        self.rule.propose(call, profile)
    }
    fn accuracy_budget(&self) -> f32 {
        self.budget
    }
    fn preserves_native_messages(&self) -> bool {
        self.rule.preserves_native_messages()
    }
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
    fn configured_rule_budgets_reach_every_rule_and_only_change_its_identity() {
        let baseline_dir = tempfile::tempdir().unwrap();
        let baseline = build_optimizer(baseline_dir.path(), OptimizerConfig::default()).unwrap();
        for name in crate::rules::KNOWN_RULE_NAMES {
            let dir = tempfile::tempdir().unwrap();
            let mut config = OptimizerConfig::default();
            config.accuracy_budget.insert(name.into(), 0.07);
            let changed = build_optimizer(dir.path(), config).unwrap();
            assert_eq!(changed.optimizer.accuracy_budget_for(name), Some(0.07));
            for rule in crate::rules::KNOWN_RULE_NAMES {
                let bind = |optimizer: &Optimizer| {
                    let mut plan = crate::planner::Plan::Rewritten {
                        rule: rule.into(),
                        call: cache_call(),
                        projected_savings_usd: 0.01,
                    };
                    optimizer.bind_rule_identities(&mut plan);
                    if let crate::planner::Plan::Rewritten { call, .. } = plan {
                        call.parameters.extra[crate::planner::RULE_IDENTITIES_KEY].clone()
                    } else {
                        unreachable!()
                    }
                };
                assert_eq!(
                    bind(&baseline.optimizer) == bind(&changed.optimizer),
                    rule != name,
                    "{name}/{rule}"
                );
            }
        }
    }

    #[test]
    fn rule_enable_flags_and_tuning_reach_production_constructors() {
        let dir = tempfile::tempdir().unwrap();
        let config: OptimizerConfig = toml::from_str(
            r#"
            [rules.OutputBudget]
            enabled = false
            [rules.ContextCompress]
            min_prompt_bytes = 2048
            min_dead_fraction = 0.6
            [rules.ParallelBranch]
            max_fanout = 8
        "#,
        )
        .unwrap();
        let wired = build_optimizer(dir.path(), config).unwrap();
        assert!(wired
            .optimizer
            .accuracy_budget_for("OutputBudget")
            .is_none());
        for (name, field, value) in [
            (
                "ContextCompress",
                "min_prompt_bytes",
                serde_json::json!(2048),
            ),
            (
                "ContextCompress",
                "min_dead_fraction",
                serde_json::json!(0.6f32),
            ),
            ("ParallelBranch", "max_fanout", serde_json::json!(8)),
        ] {
            let mut plan = crate::planner::Plan::Rewritten {
                rule: name.into(),
                call: cache_call(),
                projected_savings_usd: 0.01,
            };
            wired.optimizer.bind_rule_identities(&mut plan);
            if let crate::planner::Plan::Rewritten { call, .. } = plan {
                assert_eq!(
                    call.parameters.extra[crate::planner::RULE_IDENTITIES_KEY]["rules"][name]
                        ["parameters"]["settings"][field],
                    value
                );
            }
        }
    }

    fn cache_call() -> Call {
        serde_json::from_value(serde_json::json!({
            "call_site_id":"sdk-cache-request", "trace_id":"00".repeat(16),
            "span_id":"00".repeat(8), "model":"gpt-4o",
            "messages":[{"role":"user","content":" task "}],
            "parameters":{"max_output_tokens":128,"extra":{"agentc_route_context":{
                "provider_protocol":"openai.chat.completions.v1","provider_namespace":"openai",
                "input_tokens_upper_bound":64,"image_input":false,"tool_calling":false,
                "structured_outputs":false,"streaming":false,
                "provider_configuration":{"version":"provider-request-config-v1","supported":true,"sha256":"a".repeat(64)}
                ,"sdk_response_cache":{"version":SDK_RESPONSE_CACHE_VERSION,"supported":true,"message_sha256":"c".repeat(64),"shape_sha256":"d".repeat(64)}
            }}}
        })).unwrap()
    }

    #[test]
    fn sdk_cache_keys_separate_output_caps_and_provider_controls() {
        let key = SdkResponseKeyBuilder;
        let call = cache_call();
        let mut capped = call.clone();
        capped.parameters.max_output_tokens = Some(2048);
        assert_ne!(key.build(&call), key.build(&capped));
        let mut seeded = call.clone();
        seeded.parameters.extra["agentc_route_context"]["provider_configuration"]["sha256"] =
            Value::String("b".repeat(64));
        assert_ne!(key.build(&call), key.build(&seeded));
    }

    #[test]
    fn sdk_cache_keys_bind_protocol_shapes_and_task_contracts_without_trace_metadata() {
        let call = cache_call();
        let expected = sdk_response_cache_key(&call).unwrap();
        let mut traced = call.clone();
        traced.trace_id = [1; 16];
        traced.span_id = [2; 8];
        traced.parameters.extra["attention_scores"] = serde_json::json!([0.2]);
        assert_eq!(sdk_response_cache_key(&traced), Some(expected.clone()));
        for field in [
            "provider_namespace",
            "provider_protocol",
            "structured_output_schema_version",
            "text_action_settings_version",
        ] {
            let mut changed = call.clone();
            changed.parameters.extra[ROUTE_CONTEXT_KEY][field] =
                serde_json::json!(if field == "provider_protocol" {
                    "litellm.completion.v1"
                } else {
                    "changed"
                });
            assert_ne!(
                sdk_response_cache_key(&changed).unwrap(),
                expected,
                "{field}"
            );
        }
        let mut changed = call.clone();
        changed.parameters.extra[ROUTE_CONTEXT_KEY]["sdk_response_cache"]["shape_sha256"] =
            serde_json::json!("e".repeat(64));
        assert_ne!(sdk_response_cache_key(&changed).unwrap(), expected);
    }

    #[test]
    fn sdk_cache_keys_abstain_for_legacy_unsupported_and_incomplete_requests() {
        for field in ["provider_configuration", "sdk_response_cache"] {
            let mut call = cache_call();
            call.parameters.extra[ROUTE_CONTEXT_KEY]
                .as_object_mut()
                .unwrap()
                .remove(field);
            assert!(sdk_response_cache_key(&call).is_none());
        }
        for (field, value) in [
            ("streaming", serde_json::json!(true)),
            ("image_input", serde_json::json!(true)),
            ("provider_protocol", serde_json::json!("unknown")),
        ] {
            let mut call = cache_call();
            call.parameters.extra[ROUTE_CONTEXT_KEY][field] = value;
            assert!(sdk_response_cache_key(&call).is_none());
        }
        let mut call = cache_call();
        call.parameters.extra[ROUTE_CONTEXT_KEY]["sdk_response_cache"]["message_sha256"] =
            serde_json::json!("bad");
        assert!(sdk_response_cache_key(&call).is_none());
    }

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
