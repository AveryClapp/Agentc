//! Agentc JIT optimizer runtime.
//!
//! Shipped so far:
//! - O1 — empirical cost model + ring-buffered audit table.
//! - O2 — `Optimizer::plan` entry point, `Plan` enum, cold-path and
//!   overhead kill switch, fail-open FFI wired via `agentc-profiler`.
//!
//! Rule implementations, shadow sampling, and the accuracy-budget machine
//! ship in later beads (O3–O5).

pub mod audit;
pub mod budget;
pub mod config;
pub mod cost_model;
pub mod dag;
pub mod dag_context;
pub mod ffi;
pub mod planner;
pub mod rules;
pub mod schema;
pub mod shadow;

pub use audit::{PlanAudit, PlanKind, RING_BUFFER_CAP};
pub use budget::{Budget, BudgetEntry, DisabledEntry, SampleOutcome, BREACH_STREAK, COOLDOWN_US};
pub use config::OptimizerConfig;
pub use cost_model::{CallSiteProfile, CostModel, CostModelUpdate, WelfordStats};
pub use dag::{Call, DepSource, Message, Outcome, Parameters, Tool};
pub use dag_context::{DagContextCache, DagSpan, DEFAULT_WINDOW, MAX_TRACES_CACHED};
pub use planner::{Optimizer, Plan, Proposal, RewriteRule};
pub use rules::{
    CacheHitRule, ContextCompressRule, ModelDowngradeRoute, ModelDowngradeRule,
    ParallelBranchRule, StateDropRule,
};
pub use shadow::{
    text_divergence, tool_call_divergence, ShadowSampler, ToolCall, DEFAULT_SHADOW_RATE,
};
