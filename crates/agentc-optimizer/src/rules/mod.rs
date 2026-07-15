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
