//! Runtime configuration for the optimizer.
//!
//! Resolve defaults, the shared config.toml, then environment overrides at
//! optimizer bootstrap. Invalid declared controls disable optional optimization
//! and exploration without recording configuration contents or storage paths.

use crate::execution_plan::{SelectionObjective, SelectionPolicy};
use crate::exploration::{ExplorationAllocation, ExplorationPolicy};
use serde::{Deserialize, Serialize};
use std::{collections::BTreeMap, env, path::Path};

pub const DEFAULT_DIVERGENCE_WINDOW: u32 = 50;

/// Planner-visible tunables. Rule-specific settings live in separate
/// config structs in their owning modules.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct OptimizerConfig {
    /// Master switch. When false, `plan` always returns `PassThrough`.
    pub enabled: bool,
    /// Minimum observations before a call site is eligible for rewrites.
    pub hot_threshold: u32,
    /// Rolling sample window for cost-model fitting. Also the confidence
    /// saturation point.
    pub cost_model_window: u32,
    /// Exact newest-N execution outcomes and, independently, paired divergence
    /// samples retained for each complete execution plan.
    pub plan_profile_window: u32,
    /// Exact newest-N shadow samples retained for each rule/site divergence
    /// estimate. The raw consecutive-breach controller is independent of this
    /// diagnostic window.
    pub divergence_window: u32,
    /// Kill-switch: a plan whose work exceeds this budget is discarded in
    /// favor of `PassThrough`.
    pub max_overhead_ms: f32,
    /// Bernoulli probability that an optimized call also runs its shadow
    /// counterpart for divergence measurement.
    pub shadow_rate: f32,
    /// When false, the planner uses V1 first-match-wins logic instead of
    /// V2 composition. Controlled by `AGENTC_COMPOSE=0`.
    pub compose: bool,
    pub selection: SelectionConfig,
    pub exploration: ExplorationConfig,
    pub rules: BTreeMap<String, RuleConfig>,
    pub accuracy_budget: BTreeMap<String, f32>,
    #[serde(skip)]
    pub configuration_error: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct SelectionConfig {
    pub objective: SelectionObjective,
    pub min_plan_evidence: u32,
    pub plan_profile_freshness_hours: f64,
    pub max_rewrite_depth: usize,
    pub divergence_exposure_budget: f64,
    pub global_divergence_threshold: Option<f64>,
}

impl Default for SelectionConfig {
    fn default() -> Self {
        Self {
            objective: SelectionObjective::Cost,
            min_plan_evidence: 20,
            plan_profile_freshness_hours: 24.0,
            max_rewrite_depth: 3,
            divergence_exposure_budget: 1.0,
            global_divergence_threshold: None,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct ExplorationConfig {
    pub enabled: bool,
    pub allocation: ExplorationAllocation,
    pub calls_per_site_24h: u32,
    pub max_concurrent_counterfactuals: u32,
}

impl Default for ExplorationConfig {
    fn default() -> Self {
        Self {
            enabled: true,
            allocation: ExplorationAllocation::CompletionFirst,
            calls_per_site_24h: 20,
            max_concurrent_counterfactuals: 1,
        }
    }
}

#[derive(Debug, Default, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct RuleConfig {
    pub enabled: Option<bool>,
    pub min_prompt_bytes: Option<usize>,
    pub min_dead_fraction: Option<f32>,
    pub max_fanout: Option<usize>,
}

impl Default for OptimizerConfig {
    fn default() -> Self {
        // Debug builds run unoptimised JSON deserialization that easily takes
        // 10-20 ms per call. Release builds are much faster; 5 ms is the
        // production spec target.
        #[cfg(debug_assertions)]
        let max_overhead_ms = 50.0_f32;
        #[cfg(not(debug_assertions))]
        let max_overhead_ms = 5.0_f32;

        Self {
            enabled: true,
            hot_threshold: 3,
            cost_model_window: 50,
            plan_profile_window: 50,
            divergence_window: DEFAULT_DIVERGENCE_WINDOW,
            max_overhead_ms,
            shadow_rate: 0.02,
            compose: true,
            selection: SelectionConfig::default(),
            exploration: ExplorationConfig::default(),
            rules: BTreeMap::new(),
            accuracy_budget: BTreeMap::new(),
            configuration_error: None,
        }
    }
}

impl OptimizerConfig {
    /// Apply immutable, validated environment overrides after TOML values.
    pub fn apply_env_overrides(&mut self) {
        let result = (|| -> Result<(), String> {
            macro_rules! number {
                ($name:literal, $field:expr) => {
                    if let Ok(raw) = env::var($name) {
                        $field = raw
                            .trim()
                            .parse()
                            .map_err(|_| concat!("invalid ", $name).to_string())?;
                    }
                };
            }
            macro_rules! boolean {
                ($name:literal, $field:expr) => {
                    if let Ok(raw) = env::var($name) {
                        $field = parse_bool(raw)
                            .ok_or_else(|| concat!("invalid ", $name).to_string())?;
                    }
                };
            }
            boolean!("AGENTC_OPTIMIZE", self.enabled);
            boolean!("AGENTC_COMPOSE", self.compose);
            number!("AGENTC_OPTIMIZE_HOT_THRESHOLD", self.hot_threshold);
            number!("AGENTC_OPTIMIZE_COST_MODEL_WINDOW", self.cost_model_window);
            number!(
                "AGENTC_OPTIMIZE_PLAN_PROFILE_WINDOW",
                self.plan_profile_window
            );
            number!("AGENTC_OPTIMIZE_DIVERGENCE_WINDOW", self.divergence_window);
            number!("AGENTC_OPTIMIZE_MAX_OVERHEAD_MS", self.max_overhead_ms);
            number!("AGENTC_OPTIMIZE_SHADOW", self.shadow_rate);
            number!(
                "AGENTC_OPTIMIZE_MIN_PLAN_EVIDENCE",
                self.selection.min_plan_evidence
            );
            number!(
                "AGENTC_OPTIMIZE_PLAN_PROFILE_FRESHNESS_HOURS",
                self.selection.plan_profile_freshness_hours
            );
            number!(
                "AGENTC_OPTIMIZE_MAX_REWRITE_DEPTH",
                self.selection.max_rewrite_depth
            );
            number!(
                "AGENTC_OPTIMIZE_DIVERGENCE_EXPOSURE_BUDGET",
                self.selection.divergence_exposure_budget
            );
            boolean!("AGENTC_OPTIMIZE_EXPLORATION", self.exploration.enabled);
            number!(
                "AGENTC_OPTIMIZE_EXPLORATION_CALLS_PER_SITE_24H",
                self.exploration.calls_per_site_24h
            );
            number!(
                "AGENTC_OPTIMIZE_MAX_CONCURRENT_COUNTERFACTUALS",
                self.exploration.max_concurrent_counterfactuals
            );
            if let Ok(raw) = env::var("AGENTC_SHADOW_DIVERGENCE_BUDGET") {
                self.selection.global_divergence_threshold = Some(
                    raw.trim()
                        .parse()
                        .map_err(|_| "invalid AGENTC_SHADOW_DIVERGENCE_BUDGET")?,
                );
            }
            if let Ok(raw) = env::var("AGENTC_OPTIMIZE_OBJECTIVE") {
                self.selection.objective = match raw.trim().to_ascii_lowercase().as_str() {
                    "cost" => SelectionObjective::Cost,
                    "latency" => SelectionObjective::Latency,
                    _ => return Err("invalid AGENTC_OPTIMIZE_OBJECTIVE".into()),
                };
            }
            if let Ok(raw) = env::var("AGENTC_OPTIMIZE_EXPLORATION_ALLOCATION") {
                self.exploration.allocation = match raw.trim().to_ascii_lowercase().as_str() {
                    "completion_first" => ExplorationAllocation::CompletionFirst,
                    "least_evidence" => ExplorationAllocation::LeastEvidence,
                    _ => {
                        // Allocation is optional acquisition work. An invalid
                        // override withholds probes without discarding an
                        // independently admitted user-visible plan.
                        self.exploration.enabled = false;
                        self.exploration.allocation
                    }
                };
            }
            if let Ok(raw) = env::var("AGENTC_ENABLED_RULES") {
                let names: Vec<_> = raw
                    .split(',')
                    .map(str::trim)
                    .filter(|s| !s.is_empty())
                    .collect();
                if names
                    .iter()
                    .any(|name| !crate::rules::is_known_rule_name(name))
                {
                    return Err("invalid AGENTC_ENABLED_RULES".into());
                }
                for name in crate::rules::KNOWN_RULE_NAMES {
                    self.rules.entry(name.into()).or_default().enabled =
                        Some(names.contains(&name));
                }
            }
            self.validate()
        })();
        if let Err(error) = result {
            self.disable(error);
        }
    }

    pub fn from_storage(storage: &Path) -> Self {
        let path = env::var_os("AGENTC_CONFIG_PATH")
            .map(std::path::PathBuf::from)
            .unwrap_or_else(|| storage.join("config.toml"));
        Self::from_file(&path)
    }

    pub fn from_file(path: &Path) -> Self {
        #[derive(Default, Deserialize)]
        struct Document {
            #[serde(default)]
            optimizer: OptimizerConfig,
        }
        let mut config = match std::fs::read_to_string(path) {
            Ok(raw) => match toml::from_str::<Document>(&raw) {
                Ok(document) => document.optimizer,
                Err(_) => {
                    let mut config = Self::default();
                    config.disable("invalid optimizer configuration file".into());
                    return config;
                }
            },
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Self::default(),
            Err(_) => {
                let mut config = Self::default();
                config.disable("unreadable optimizer configuration file".into());
                return config;
            }
        };
        config.apply_env_overrides();
        config
    }

    fn disable(&mut self, error: String) {
        *self = Self::default();
        self.enabled = false;
        self.exploration.enabled = false;
        self.configuration_error = Some(error);
    }

    pub fn validate(&self) -> Result<(), String> {
        if [
            self.hot_threshold,
            self.cost_model_window,
            self.plan_profile_window,
            self.divergence_window,
            self.selection.min_plan_evidence,
            self.exploration.calls_per_site_24h,
            self.exploration.max_concurrent_counterfactuals,
        ]
        .contains(&0)
        {
            return Err("optimizer windows, evidence and call limits must be positive".into());
        }
        if !self.max_overhead_ms.is_finite()
            || self.max_overhead_ms < 0.0
            || !self.shadow_rate.is_finite()
            || !(0.0..=1.0).contains(&self.shadow_rate)
            || !self.selection.plan_profile_freshness_hours.is_finite()
            || self.selection.plan_profile_freshness_hours <= 0.0
            || self.selection.plan_profile_freshness_hours * 3_600_000_000.0 > i64::MAX as f64
            || !(1..=crate::planner::MAX_JOINT_REWRITE_DEPTH)
                .contains(&self.selection.max_rewrite_depth)
            || !self.selection.divergence_exposure_budget.is_finite()
            || self.selection.divergence_exposure_budget < 0.0
            || self
                .selection
                .global_divergence_threshold
                .is_some_and(|v| !v.is_finite() || !(0.0..=1.0).contains(&v))
        {
            return Err("invalid optimizer planning or safety limits".into());
        }
        for (name, settings) in &self.rules {
            if !crate::rules::is_known_rule_name(name)
                || (name != "ContextCompress"
                    && (settings.min_prompt_bytes.is_some()
                        || settings.min_dead_fraction.is_some()))
                || (name != "ParallelBranch" && settings.max_fanout.is_some())
                || settings.min_prompt_bytes == Some(0)
                || settings
                    .min_dead_fraction
                    .is_some_and(|v| !v.is_finite() || !(0.0..=1.0).contains(&v))
                || settings.max_fanout.is_some_and(|v| v < 2)
            {
                return Err("invalid optimizer rule settings".into());
            }
        }
        if self.accuracy_budget.iter().any(|(name, value)| {
            !crate::rules::is_known_rule_name(name)
                || !value.is_finite()
                || !(0.0..=1.0).contains(value)
        }) {
            return Err("invalid optimizer accuracy budgets".into());
        }
        Ok(())
    }

    pub fn selection_policy(&self, now_us: i64) -> SelectionPolicy {
        SelectionPolicy {
            objective: self.selection.objective,
            min_paired_observations: self.selection.min_plan_evidence,
            max_profile_age_us: (self.selection.plan_profile_freshness_hours * 3_600_000_000.0)
                as i64,
            divergence_exposure_budget: self.selection.divergence_exposure_budget,
            now_us,
            ..SelectionPolicy::default()
        }
    }

    pub fn exploration_policy(&self) -> ExplorationPolicy {
        ExplorationPolicy {
            allocation: self.exploration.allocation,
            max_calls_per_site: self.exploration.calls_per_site_24h,
            max_concurrent_per_site: self.exploration.max_concurrent_counterfactuals,
            evidence_target: self.selection.min_plan_evidence,
            divergence_exposure_budget: self.selection.divergence_exposure_budget,
            ..ExplorationPolicy::default()
        }
    }

    /// Build a config from defaults + env overrides.
    pub fn from_env() -> Self {
        let mut c = Self::default();
        c.apply_env_overrides();
        c
    }
}

fn parse_bool(s: String) -> Option<bool> {
    match s.trim().to_ascii_lowercase().as_str() {
        "1" | "true" | "yes" | "on" => Some(true),
        "0" | "false" | "no" | "off" => Some(false),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_matches_spec() {
        let c = OptimizerConfig::default();
        assert!(c.enabled);
        assert_eq!(c.hot_threshold, 3);
        assert_eq!(c.cost_model_window, 50);
        assert_eq!(c.plan_profile_window, 50);
        assert_eq!(c.divergence_window, 50);
        // Debug builds use 50 ms; release builds use 5 ms.
        #[cfg(debug_assertions)]
        assert!((c.max_overhead_ms - 50.0).abs() < 1e-6);
        #[cfg(not(debug_assertions))]
        assert!((c.max_overhead_ms - 5.0).abs() < 1e-6);
        assert!((c.shadow_rate - 0.02).abs() < 1e-6);
    }

    #[test]
    fn parse_bool_accepts_common_forms() {
        assert_eq!(parse_bool("1".into()), Some(true));
        assert_eq!(parse_bool("TRUE".into()), Some(true));
        assert_eq!(parse_bool("no".into()), Some(false));
        assert_eq!(parse_bool("".into()), None);
        assert_eq!(parse_bool("maybe".into()), None);
    }

    #[test]
    fn toml_rule_settings_and_selector_controls_are_loaded_and_validated() {
        let parsed: OptimizerConfig = toml::from_str(
            r#"
            hot_threshold = 7
            [selection]
            objective = "latency"
            min_plan_evidence = 25
            [exploration]
            calls_per_site_24h = 30
            allocation = "least_evidence"
            [rules.ContextCompress]
            min_prompt_bytes = 4096
            min_dead_fraction = 0.4
            [rules.OutputBudget]
            enabled = false
            [accuracy_budget]
            ContextCompress = 0.01
        "#,
        )
        .unwrap();
        parsed.validate().unwrap();
        assert_eq!(parsed.hot_threshold, 7);
        assert_eq!(
            parsed.selection_policy(10).objective,
            SelectionObjective::Latency
        );
        assert_eq!(parsed.exploration_policy().max_calls_per_site, 30);
        assert_eq!(parsed.rules["ContextCompress"].min_prompt_bytes, Some(4096));
        assert_eq!(parsed.rules["OutputBudget"].enabled, Some(false));
        assert_eq!(parsed.accuracy_budget["ContextCompress"], 0.01);
    }

    #[test]
    fn undeclared_rule_controls_and_invalid_risk_values_are_rejected() {
        for raw in [
            "hot_thresold = 7",
            "[rules.ContextCompress]\nroute = []",
            "[rules.OutputBudget]\nmax_fanout = 4",
            "[rules.Unknown]\nenabled = true",
            "[accuracy_budget]\nUnknown = 0.1",
            "[accuracy_budget]\nCacheHit = nan",
            "[accuracy_budget]\nCacheHit = -0.1",
            "[rules.ParallelBranch]\nmax_fanout = 1",
            "[rules.ContextCompress]\nmin_dead_fraction = 1.1",
            "[selection]\nmin_plan_evidence = 0",
            "shadow_rate = inf",
        ] {
            assert!(
                !toml::from_str::<OptimizerConfig>(raw)
                    .is_ok_and(|config| config.validate().is_ok()),
                "{raw}"
            );
        }
    }
}
