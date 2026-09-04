//! Runtime configuration for the optimizer.
//!
//! The configuration is resolved once when the optimizer starts and is held
//! immutably thereafter. Environment overrides are deliberately strict: an
//! invalid value disables optimization and exploration for that process rather
//! than silently changing the risk contract.

use std::env;
use std::error::Error;
use std::fmt;

use crate::execution_plan::{SelectionObjective, SelectionPolicy};
use crate::exploration::{
    ExplorationPolicy, DEFAULT_CONCURRENT_COUNTERFACTUAL_CAP, DEFAULT_EXPLORATION_CALL_CAP,
    DEFAULT_EXPLORATION_LEASE_US, DEFAULT_EXPLORATION_SEED, EXPLORATION_WINDOW_US,
};
use crate::plan_guard::DEFAULT_PLAN_EXPOSURE_BUDGET;
use crate::planner::MAX_JOINT_REWRITE_DEPTH;

pub const DEFAULT_DIVERGENCE_WINDOW: u32 = 50;
pub const DEFAULT_MIN_PLAN_EVIDENCE: u32 = 20;
pub const DEFAULT_PLAN_PROFILE_FRESHNESS_HOURS: f64 = 24.0;
pub const DEFAULT_EVALUATION_TASK_DAMAGE_BUDGET: f64 = 5.0;
pub const TAU2_NON_INFERIORITY_MARGIN: f64 = -0.03;
pub const SWE_BENCH_NON_INFERIORITY_MARGIN: f64 = -0.02;
pub const OSWORLD_NON_INFERIORITY_MARGIN: f64 = -0.02;

const MICROS_PER_HOUR: f64 = 3_600_000_000.0;

/// Planner-visible tunables. Rule-specific settings live in separate config
/// structs in their owning modules.
#[derive(Debug, Clone, PartialEq)]
pub struct OptimizerConfig {
    /// Master switch. When false, planning always returns the reference call.
    pub enabled: bool,
    /// Minimum observations before a call site is eligible for rewrites.
    pub hot_threshold: u32,
    /// Rolling sample window for cost-model fitting.
    pub cost_model_window: u32,
    /// Exact newest-N execution outcomes and paired divergences per plan.
    pub plan_profile_window: u32,
    /// Exact newest-N shadow samples retained per rule and call site.
    pub divergence_window: u32,
    /// Kill switch over synchronous planning work.
    pub max_overhead_ms: f32,
    /// Probability that an admitted call also runs its reference counterpart.
    pub shadow_rate: f32,
    /// Whether compatible semantic rewrites may be composed.
    pub compose: bool,
    /// Cost-first or latency-first complete-plan selection.
    pub objective: SelectionObjective,
    /// Paired complete-plan observations required for user-visible admission.
    pub min_plan_evidence: u32,
    /// Maximum age of the newest paired observation.
    pub plan_profile_freshness_hours: f64,
    /// Maximum semantic rewrite count in one candidate.
    pub max_rewrite_depth: usize,
    /// Whether cold plans may acquire evidence through billed counterfactuals.
    pub exploration_enabled: bool,
    /// Counterfactual call cap per semantic call site in a rolling 24 hours.
    pub exploration_calls_per_site_24h: u32,
    /// Live counterfactual cap per semantic call site.
    pub max_concurrent_counterfactuals: u32,
    /// Runtime divergence-exposure budget for one exact complete plan.
    pub divergence_exposure_budget: f64,
    /// Optional global output-divergence threshold. When absent, the strictest
    /// constituent rule threshold applies to each complete plan.
    pub global_divergence_threshold: Option<f64>,
    /// Evaluation-only task-equivalent damage ceiling. Production does not
    /// observe task quality, so this is enforced only for labeled feedback.
    pub evaluation_task_damage_budget: f64,
    /// Workload-specific evaluation margin. There is no universal production
    /// default; campaign workers set it from their frozen workload contract.
    pub evaluation_non_inferiority_margin: Option<f64>,
    /// Present only when strict environment resolution failed. The resulting
    /// config is disabled but remains inspectable.
    pub configuration_error: Option<String>,
}

impl Default for OptimizerConfig {
    fn default() -> Self {
        Self {
            enabled: true,
            hot_threshold: 3,
            cost_model_window: 50,
            plan_profile_window: 50,
            divergence_window: DEFAULT_DIVERGENCE_WINDOW,
            max_overhead_ms: 5.0,
            shadow_rate: 0.02,
            compose: true,
            objective: SelectionObjective::Cost,
            min_plan_evidence: DEFAULT_MIN_PLAN_EVIDENCE,
            plan_profile_freshness_hours: DEFAULT_PLAN_PROFILE_FRESHNESS_HOURS,
            max_rewrite_depth: MAX_JOINT_REWRITE_DEPTH,
            exploration_enabled: true,
            exploration_calls_per_site_24h: DEFAULT_EXPLORATION_CALL_CAP,
            max_concurrent_counterfactuals: DEFAULT_CONCURRENT_COUNTERFACTUAL_CAP,
            divergence_exposure_budget: DEFAULT_PLAN_EXPOSURE_BUDGET,
            global_divergence_threshold: None,
            evaluation_task_damage_budget: DEFAULT_EVALUATION_TASK_DAMAGE_BUDGET,
            evaluation_non_inferiority_margin: None,
            configuration_error: None,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum OptimizerConfigError {
    InvalidEnvironment {
        variable: &'static str,
        expected: &'static str,
    },
    InvalidField {
        field: &'static str,
        expected: &'static str,
    },
}

impl fmt::Display for OptimizerConfigError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidEnvironment { variable, expected } => {
                write!(formatter, "{variable} must be {expected}")
            }
            Self::InvalidField { field, expected } => {
                write!(formatter, "optimizer.{field} must be {expected}")
            }
        }
    }
}

impl Error for OptimizerConfigError {}

impl OptimizerConfig {
    /// Apply strict environment overrides and validate the complete contract.
    /// A present but malformed variable is an error; it is never silently
    /// treated as if the operator had not set it.
    pub fn apply_env_overrides(&mut self) -> Result<(), OptimizerConfigError> {
        apply_bool("AGENTC_OPTIMIZE", &mut self.enabled)?;
        apply_parsed(
            "AGENTC_OPTIMIZE_HOT_THRESHOLD",
            "a positive integer",
            &mut self.hot_threshold,
        )?;
        apply_parsed(
            "AGENTC_OPTIMIZE_COST_MODEL_WINDOW",
            "a positive integer",
            &mut self.cost_model_window,
        )?;
        apply_parsed(
            "AGENTC_OPTIMIZE_PLAN_PROFILE_WINDOW",
            "a positive integer",
            &mut self.plan_profile_window,
        )?;
        apply_parsed(
            "AGENTC_OPTIMIZE_DIVERGENCE_WINDOW",
            "a positive integer",
            &mut self.divergence_window,
        )?;
        apply_parsed(
            "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS",
            "a finite non-negative number",
            &mut self.max_overhead_ms,
        )?;
        apply_parsed(
            "AGENTC_OPTIMIZE_SHADOW",
            "a finite fraction in [0, 1]",
            &mut self.shadow_rate,
        )?;
        apply_bool("AGENTC_COMPOSE", &mut self.compose)?;
        apply_objective("AGENTC_OPTIMIZE_OBJECTIVE", &mut self.objective)?;
        apply_parsed(
            "AGENTC_OPTIMIZE_MIN_PLAN_EVIDENCE",
            "a positive integer",
            &mut self.min_plan_evidence,
        )?;
        apply_parsed(
            "AGENTC_OPTIMIZE_PLAN_PROFILE_FRESHNESS_HOURS",
            "a finite positive number",
            &mut self.plan_profile_freshness_hours,
        )?;
        apply_parsed(
            "AGENTC_OPTIMIZE_MAX_REWRITE_DEPTH",
            "an integer in [1, 3]",
            &mut self.max_rewrite_depth,
        )?;
        apply_bool("AGENTC_OPTIMIZE_EXPLORATION", &mut self.exploration_enabled)?;
        apply_parsed(
            "AGENTC_OPTIMIZE_EXPLORATION_CALLS_PER_SITE_24H",
            "a positive integer",
            &mut self.exploration_calls_per_site_24h,
        )?;
        apply_parsed(
            "AGENTC_OPTIMIZE_MAX_CONCURRENT_COUNTERFACTUALS",
            "a positive integer",
            &mut self.max_concurrent_counterfactuals,
        )?;
        apply_parsed(
            "AGENTC_OPTIMIZE_DIVERGENCE_EXPOSURE_BUDGET",
            "a finite non-negative number",
            &mut self.divergence_exposure_budget,
        )?;
        apply_optional_parsed(
            "AGENTC_SHADOW_DIVERGENCE_BUDGET",
            "a finite fraction in [0, 1]",
            &mut self.global_divergence_threshold,
        )?;
        apply_parsed(
            "AGENTC_OPTIMIZE_TASK_DAMAGE_BUDGET",
            "a finite non-negative number",
            &mut self.evaluation_task_damage_budget,
        )?;
        apply_optional_parsed(
            "AGENTC_OPTIMIZE_NON_INFERIORITY_MARGIN",
            "a finite number in [-1, 0]",
            &mut self.evaluation_non_inferiority_margin,
        )?;
        self.validate()
    }

    /// Resolve defaults plus environment overrides. Any malformed value turns
    /// the optimizer and exploration off, preserving the original request.
    pub fn from_env() -> Self {
        match Self::try_from_env() {
            Ok(config) => config,
            Err(error) => {
                eprintln!("[agentc-optimizer] invalid configuration; disabled: {error}");
                Self::safe_disabled(error.to_string())
            }
        }
    }

    pub fn try_from_env() -> Result<Self, OptimizerConfigError> {
        let mut config = Self::default();
        config.apply_env_overrides()?;
        Ok(config)
    }

    pub fn safe_disabled(reason: impl Into<String>) -> Self {
        Self {
            enabled: false,
            exploration_enabled: false,
            configuration_error: Some(reason.into()),
            ..Self::default()
        }
    }

    pub fn validate(&self) -> Result<(), OptimizerConfigError> {
        require_positive_u32("hot_threshold", self.hot_threshold)?;
        require_positive_u32("cost_model_window", self.cost_model_window)?;
        require_positive_u32("plan_profile_window", self.plan_profile_window)?;
        require_positive_u32("divergence_window", self.divergence_window)?;
        if !self.max_overhead_ms.is_finite() || self.max_overhead_ms < 0.0 {
            return invalid("max_overhead_ms", "finite and non-negative");
        }
        if !self.shadow_rate.is_finite() || !(0.0..=1.0).contains(&self.shadow_rate) {
            return invalid("shadow_rate", "a finite fraction in [0, 1]");
        }
        require_positive_u32("min_plan_evidence", self.min_plan_evidence)?;
        if self.min_plan_evidence > self.plan_profile_window {
            return invalid("min_plan_evidence", "no larger than plan_profile_window");
        }
        if !self.plan_profile_freshness_hours.is_finite()
            || self.plan_profile_freshness_hours <= 0.0
            || self.plan_profile_freshness_hours * MICROS_PER_HOUR > i64::MAX as f64
        {
            return invalid(
                "plan_profile_freshness_hours",
                "finite, positive, and representable in microseconds",
            );
        }
        if !(1..=MAX_JOINT_REWRITE_DEPTH).contains(&self.max_rewrite_depth) {
            return invalid("max_rewrite_depth", "in [1, 3]");
        }
        require_positive_u32(
            "exploration_calls_per_site_24h",
            self.exploration_calls_per_site_24h,
        )?;
        require_positive_u32(
            "max_concurrent_counterfactuals",
            self.max_concurrent_counterfactuals,
        )?;
        if !is_nonnegative_finite(self.divergence_exposure_budget) {
            return invalid("divergence_exposure_budget", "finite and non-negative");
        }
        if self
            .global_divergence_threshold
            .is_some_and(|threshold| !threshold.is_finite() || !(0.0..=1.0).contains(&threshold))
        {
            return invalid("global_divergence_threshold", "a finite fraction in [0, 1]");
        }
        if !is_nonnegative_finite(self.evaluation_task_damage_budget) {
            return invalid("evaluation_task_damage_budget", "finite and non-negative");
        }
        if self
            .evaluation_non_inferiority_margin
            .is_some_and(|margin| !margin.is_finite() || !(-1.0..=0.0).contains(&margin))
        {
            return invalid(
                "evaluation_non_inferiority_margin",
                "a finite number in [-1, 0]",
            );
        }
        Ok(())
    }

    pub fn selection_policy(&self, now_us: i64) -> SelectionPolicy {
        SelectionPolicy {
            objective: self.objective,
            min_paired_observations: self.min_plan_evidence,
            now_us,
            max_profile_age_us: (self.plan_profile_freshness_hours * MICROS_PER_HOUR) as i64,
            divergence_exposure_budget: self.divergence_exposure_budget,
            max_expected_cost_usd: None,
            max_expected_latency_ms: None,
        }
    }

    pub fn exploration_policy(&self) -> ExplorationPolicy {
        ExplorationPolicy {
            seed: DEFAULT_EXPLORATION_SEED,
            max_calls_per_site: self.exploration_calls_per_site_24h,
            max_concurrent_per_site: self.max_concurrent_counterfactuals,
            evidence_target: self.min_plan_evidence,
            window_us: EXPLORATION_WINDOW_US,
            lease_duration_us: DEFAULT_EXPLORATION_LEASE_US,
            divergence_exposure_budget: self.divergence_exposure_budget,
            task_damage_budget: Some(self.evaluation_task_damage_budget),
        }
    }
}

fn invalid<T>(field: &'static str, expected: &'static str) -> Result<T, OptimizerConfigError> {
    Err(OptimizerConfigError::InvalidField { field, expected })
}

fn require_positive_u32(field: &'static str, value: u32) -> Result<(), OptimizerConfigError> {
    if value == 0 {
        invalid(field, "positive")
    } else {
        Ok(())
    }
}

fn is_nonnegative_finite(value: f64) -> bool {
    value.is_finite() && value >= 0.0
}

fn apply_bool(name: &'static str, destination: &mut bool) -> Result<(), OptimizerConfigError> {
    let Ok(raw) = env::var(name) else {
        return Ok(());
    };
    *destination = parse_bool(&raw).ok_or(OptimizerConfigError::InvalidEnvironment {
        variable: name,
        expected: "a boolean (0/1, false/true, no/yes, or off/on)",
    })?;
    Ok(())
}

fn apply_objective(
    name: &'static str,
    destination: &mut SelectionObjective,
) -> Result<(), OptimizerConfigError> {
    let Ok(raw) = env::var(name) else {
        return Ok(());
    };
    *destination = match raw.trim().to_ascii_lowercase().as_str() {
        "cost" => SelectionObjective::Cost,
        "latency" => SelectionObjective::Latency,
        _ => {
            return Err(OptimizerConfigError::InvalidEnvironment {
                variable: name,
                expected: "cost or latency",
            });
        }
    };
    Ok(())
}

fn apply_parsed<T: std::str::FromStr>(
    name: &'static str,
    expected: &'static str,
    destination: &mut T,
) -> Result<(), OptimizerConfigError> {
    let Ok(raw) = env::var(name) else {
        return Ok(());
    };
    *destination = raw
        .trim()
        .parse()
        .map_err(|_| OptimizerConfigError::InvalidEnvironment {
            variable: name,
            expected,
        })?;
    Ok(())
}

fn apply_optional_parsed<T: std::str::FromStr>(
    name: &'static str,
    expected: &'static str,
    destination: &mut Option<T>,
) -> Result<(), OptimizerConfigError> {
    let Ok(raw) = env::var(name) else {
        return Ok(());
    };
    *destination =
        Some(
            raw.trim()
                .parse()
                .map_err(|_| OptimizerConfigError::InvalidEnvironment {
                    variable: name,
                    expected,
                })?,
        );
    Ok(())
}

fn parse_bool(value: &str) -> Option<bool> {
    match value.trim().to_ascii_lowercase().as_str() {
        "1" | "true" | "yes" | "on" => Some(true),
        "0" | "false" | "no" | "off" => Some(false),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::ffi::OsString;
    use std::sync::Mutex;

    static ENV_LOCK: Mutex<()> = Mutex::new(());

    struct EnvRestore {
        name: &'static str,
        value: Option<OsString>,
    }

    impl Drop for EnvRestore {
        fn drop(&mut self) {
            match self.value.take() {
                Some(value) => env::set_var(self.name, value),
                None => env::remove_var(self.name),
            }
        }
    }

    #[test]
    fn default_matches_frozen_contract() {
        let config = OptimizerConfig::default();
        assert!(config.enabled);
        assert_eq!(config.hot_threshold, 3);
        assert_eq!(config.cost_model_window, 50);
        assert_eq!(config.plan_profile_window, 50);
        assert_eq!(config.divergence_window, 50);
        assert_eq!(config.max_overhead_ms, 5.0);
        assert_eq!(config.shadow_rate, 0.02);
        assert!(config.compose);
        assert_eq!(config.objective, SelectionObjective::Cost);
        assert_eq!(config.min_plan_evidence, 20);
        assert_eq!(config.plan_profile_freshness_hours, 24.0);
        assert_eq!(config.max_rewrite_depth, 3);
        assert!(config.exploration_enabled);
        assert_eq!(config.exploration_calls_per_site_24h, 20);
        assert_eq!(config.max_concurrent_counterfactuals, 1);
        assert_eq!(config.divergence_exposure_budget, 1.0);
        assert_eq!(config.global_divergence_threshold, None);
        assert_eq!(config.evaluation_task_damage_budget, 5.0);
        assert_eq!(config.evaluation_non_inferiority_margin, None);
        assert_eq!(config.configuration_error, None);
        config.validate().unwrap();
    }

    #[test]
    fn derived_policies_share_one_risk_contract() {
        let config = OptimizerConfig {
            objective: SelectionObjective::Latency,
            min_plan_evidence: 7,
            plan_profile_window: 12,
            plan_profile_freshness_hours: 2.5,
            exploration_calls_per_site_24h: 11,
            max_concurrent_counterfactuals: 2,
            divergence_exposure_budget: 0.4,
            evaluation_task_damage_budget: 3.0,
            ..OptimizerConfig::default()
        };
        config.validate().unwrap();
        let selection = config.selection_policy(17);
        assert_eq!(selection.objective, SelectionObjective::Latency);
        assert_eq!(selection.min_paired_observations, 7);
        assert_eq!(selection.max_profile_age_us, 9_000_000_000);
        assert_eq!(selection.divergence_exposure_budget, 0.4);
        let exploration = config.exploration_policy();
        assert_eq!(exploration.evidence_target, 7);
        assert_eq!(exploration.max_calls_per_site, 11);
        assert_eq!(exploration.max_concurrent_per_site, 2);
        assert_eq!(exploration.divergence_exposure_budget, 0.4);
        assert_eq!(exploration.task_damage_budget, Some(3.0));
    }

    #[test]
    fn every_numeric_boundary_is_validated() {
        let invalid = [
            OptimizerConfig {
                hot_threshold: 0,
                ..OptimizerConfig::default()
            },
            OptimizerConfig {
                max_overhead_ms: f32::NAN,
                ..OptimizerConfig::default()
            },
            OptimizerConfig {
                shadow_rate: 1.01,
                ..OptimizerConfig::default()
            },
            OptimizerConfig {
                min_plan_evidence: 51,
                ..OptimizerConfig::default()
            },
            OptimizerConfig {
                plan_profile_freshness_hours: f64::INFINITY,
                ..OptimizerConfig::default()
            },
            OptimizerConfig {
                max_rewrite_depth: 4,
                ..OptimizerConfig::default()
            },
            OptimizerConfig {
                divergence_exposure_budget: -0.1,
                ..OptimizerConfig::default()
            },
            OptimizerConfig {
                global_divergence_threshold: Some(1.01),
                ..OptimizerConfig::default()
            },
            OptimizerConfig {
                evaluation_task_damage_budget: f64::NAN,
                ..OptimizerConfig::default()
            },
            OptimizerConfig {
                evaluation_non_inferiority_margin: Some(0.01),
                ..OptimizerConfig::default()
            },
        ];
        for config in invalid {
            assert!(config.validate().is_err(), "accepted {config:?}");
        }
    }

    #[test]
    fn safe_disabled_preserves_error_for_diagnostics() {
        let config = OptimizerConfig::safe_disabled("bad objective");
        assert!(!config.enabled);
        assert!(!config.exploration_enabled);
        assert_eq!(config.configuration_error.as_deref(), Some("bad objective"));
        config.validate().unwrap();
    }

    #[test]
    fn malformed_environment_fails_safe_instead_of_using_a_default() {
        let _lock = ENV_LOCK.lock().unwrap();
        let name = "AGENTC_OPTIMIZE_OBJECTIVE";
        let _restore = EnvRestore {
            name,
            value: env::var_os(name),
        };
        env::set_var(name, "fastest-ish");

        let config = OptimizerConfig::from_env();
        assert!(!config.enabled);
        assert!(!config.exploration_enabled);
        assert!(config
            .configuration_error
            .as_deref()
            .is_some_and(|error| error.contains(name)));
    }

    #[test]
    fn malformed_global_divergence_threshold_fails_safe() {
        let _lock = ENV_LOCK.lock().unwrap();
        let name = "AGENTC_SHADOW_DIVERGENCE_BUDGET";
        let _restore = EnvRestore {
            name,
            value: env::var_os(name),
        };
        env::set_var(name, "NaN");

        let config = OptimizerConfig::from_env();
        assert!(!config.enabled);
        assert!(!config.exploration_enabled);
        assert!(config
            .configuration_error
            .as_deref()
            .is_some_and(|error| error.contains("global_divergence_threshold")));
    }

    #[test]
    fn parse_bool_accepts_common_forms() {
        assert_eq!(parse_bool("1"), Some(true));
        assert_eq!(parse_bool("TRUE"), Some(true));
        assert_eq!(parse_bool("no"), Some(false));
        assert_eq!(parse_bool(""), None);
        assert_eq!(parse_bool("maybe"), None);
    }
}
