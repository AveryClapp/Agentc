//! The nine rewrite rules.
//!
//! Each submodule implements one rule; the planner (see `super::planner`)
//! collects proposals via the shared `RewriteRule` trait and, by default,
//! composes orthogonal rules into a single `Plan::Composed` (see
//! `super::composition`). With `AGENTC_COMPOSE=0` it falls back to V1
//! behavior: rank proposals by projected savings and pick the first that
//! passes its safety check.
//!
//! Rule-specific configuration lives on each rule's struct rather than in
//! the global `OptimizerConfig` so adding or retiring a rule doesn't
//! churn a shared shape.

pub mod cache_hit;
pub mod context_compress;
pub mod dead_output_truncation;
pub mod model_downgrade;
pub mod output_budget;
pub mod parallel_branch;
pub mod prompt_dedup;
pub mod state_drop;
pub mod structured_truncation;

pub use cache_hit::CacheHitRule;
pub use context_compress::ContextCompressRule;
pub use dead_output_truncation::DeadOutputTruncationRule;
pub use model_downgrade::{ModelDowngradeRoute, ModelDowngradeRule};
pub use output_budget::OutputBudgetRule;
pub use parallel_branch::ParallelBranchRule;
pub use prompt_dedup::PromptDedupRule;
pub use state_drop::StateDropRule;
pub use structured_truncation::StructuredTruncationRule;

/// Canonical names of the nine rewrite rules, exactly as each rule's
/// `RewriteRule::name()` returns them. The `agentc optimize disable` CLI
/// validates `--rule` against this set so a typo fails loudly instead of
/// writing a disable entry that silently never matches a real rule. Kept in
/// sync with the `name()` impls by `known_rule_names_match_rule_impls`.
pub const KNOWN_RULE_NAMES: [&str; 9] = [
    "CacheHit",
    "ContextCompress",
    "ModelDowngrade",
    "ParallelBranch",
    "StateDrop",
    "PromptDedup",
    "OutputBudget",
    "StructuredTruncation",
    "DeadOutputTruncation",
];

/// True if `name` is one of the nine rewrite-rule names.
pub fn is_known_rule_name(name: &str) -> bool {
    KNOWN_RULE_NAMES.contains(&name)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::planner::RewriteRule;

    #[test]
    fn known_rule_names_match_rule_impls() {
        // Every Default-constructible rule reports a name in the set. Guards
        // against a rule's name() drifting away from KNOWN_RULE_NAMES.
        let default_rule_names: [&str; 7] = [
            ContextCompressRule::default().name(),
            ParallelBranchRule::default().name(),
            StateDropRule::default().name(),
            PromptDedupRule::default().name(),
            OutputBudgetRule::default().name(),
            StructuredTruncationRule::default().name(),
            DeadOutputTruncationRule::default().name(),
        ];
        for n in default_rule_names {
            assert!(
                KNOWN_RULE_NAMES.contains(&n),
                "rule name {n:?} missing from KNOWN_RULE_NAMES"
            );
        }
        // CacheHit and ModelDowngrade need construction args; assert by name.
        assert!(KNOWN_RULE_NAMES.contains(&"CacheHit"));
        assert!(KNOWN_RULE_NAMES.contains(&"ModelDowngrade"));
        assert_eq!(KNOWN_RULE_NAMES.len(), 9);
    }
}
