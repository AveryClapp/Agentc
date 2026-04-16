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
pub mod config;
pub mod cost_model;
pub mod dag;
pub mod ffi;
pub mod planner;
pub mod schema;

pub use audit::{PlanAudit, PlanKind, RING_BUFFER_CAP};
pub use config::OptimizerConfig;
pub use cost_model::{CallSiteProfile, CostModel, CostModelUpdate, WelfordStats};
pub use dag::{Call, DepSource, Message, Outcome, Parameters, Tool};
pub use planner::{Optimizer, Plan, Proposal, RewriteRule};
