//! Runtime configuration for the optimizer.
//!
//! Loaded once at `Optimizer::new` and held immutably thereafter. The
//! spec's source-of-truth is `agentc.toml [optimizer]`; env vars override
//! (see `AGENTC_OPTIMIZE*` in the spec). This module owns only the
//! *planner*'s tunables — accuracy-budget / rule-routing config lives with
//! its respective bead.

use std::env;

/// Planner-visible tunables. Rule-specific settings live in separate
/// config structs in their owning modules.
#[derive(Debug, Clone, PartialEq)]
pub struct OptimizerConfig {
    /// Master switch. When false, `plan` always returns `PassThrough`.
    pub enabled: bool,
    /// Minimum observations before a call site is eligible for rewrites.
    pub hot_threshold: u32,
    /// Rolling sample window for cost-model fitting. Also the confidence
    /// saturation point.
    pub cost_model_window: u32,
    /// Kill-switch: a plan whose work exceeds this budget is discarded in
    /// favor of `PassThrough`.
    pub max_overhead_ms: f32,
    /// Bernoulli probability that an optimized call also runs its shadow
    /// counterpart for divergence measurement.
    pub shadow_rate: f32,
    /// When false, the planner uses V1 first-match-wins logic instead of
    /// V2 composition. Controlled by `AGENTC_COMPOSE=0`.
    pub compose: bool,
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
            max_overhead_ms,
            shadow_rate: 0.02,
            compose: true,
        }
    }
}

impl OptimizerConfig {
    /// Apply `AGENTC_OPTIMIZE*` environment overrides to an existing
    /// config. Unset or unparseable vars leave the field unchanged.
    ///
    /// | Variable | Field |
    /// |---|---|
    /// | `AGENTC_OPTIMIZE` | `enabled` (0/false disables; 1/true enables) |
    /// | `AGENTC_OPTIMIZE_HOT_THRESHOLD` | `hot_threshold` |
    /// | `AGENTC_OPTIMIZE_COST_MODEL_WINDOW` | `cost_model_window` |
    /// | `AGENTC_OPTIMIZE_MAX_OVERHEAD_MS` | `max_overhead_ms` |
    /// | `AGENTC_OPTIMIZE_SHADOW` | `shadow_rate` |
    /// | `AGENTC_COMPOSE` | `compose` (0/false = V1 first-match; 1/true = V2 compose) |
    pub fn apply_env_overrides(&mut self) {
        if let Some(v) = env::var("AGENTC_OPTIMIZE").ok().and_then(parse_bool) {
            self.enabled = v;
        }
        if let Some(v) = env::var("AGENTC_OPTIMIZE_HOT_THRESHOLD")
            .ok()
            .and_then(|s| s.parse().ok())
        {
            self.hot_threshold = v;
        }
        if let Some(v) = env::var("AGENTC_OPTIMIZE_COST_MODEL_WINDOW")
            .ok()
            .and_then(|s| s.parse().ok())
        {
            self.cost_model_window = v;
        }
        if let Some(v) = env::var("AGENTC_OPTIMIZE_MAX_OVERHEAD_MS")
            .ok()
            .and_then(|s| s.parse().ok())
        {
            self.max_overhead_ms = v;
        }
        if let Some(v) = env::var("AGENTC_OPTIMIZE_SHADOW")
            .ok()
            .and_then(|s| s.parse().ok())
        {
            self.shadow_rate = v;
        }
        if let Some(v) = env::var("AGENTC_COMPOSE").ok().and_then(parse_bool) {
            self.compose = v;
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
}
