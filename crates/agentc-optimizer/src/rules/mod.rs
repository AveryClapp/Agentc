//! The five rewrite rules.
//!
//! Each submodule implements one rule; the planner composes them via the
//! shared `RewriteRule` trait (see `super::planner`). Rules never compose
//! in a single plan — the planner ranks proposals by projected savings
//! and picks the first that passes its safety check.
//!
//! Rule-specific configuration lives on each rule's struct rather than in
//! the global `OptimizerConfig` so adding or retiring a rule doesn't
//! churn a shared shape.

pub mod cache_hit;
pub mod context_compress;
pub mod model_downgrade;
pub mod output_budget;
pub mod parallel_branch;
pub mod prompt_dedup;
pub mod state_drop;
pub mod structured_truncation;

pub use cache_hit::CacheHitRule;
pub use context_compress::ContextCompressRule;
pub use model_downgrade::{ModelDowngradeRoute, ModelDowngradeRule};
pub use output_budget::OutputBudgetRule;
pub use parallel_branch::ParallelBranchRule;
pub use prompt_dedup::PromptDedupRule;
pub use state_drop::StateDropRule;
pub use structured_truncation::StructuredTruncationRule;
