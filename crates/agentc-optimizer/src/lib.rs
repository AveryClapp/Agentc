//! Agentc JIT optimizer runtime.
//!
//! O1 scope: the empirical cost model and the audit ring buffer. The rule
//! engine, shadow sampling, and the `optimize_plan` entry point ship in
//! later beads; this crate exists now so the profiler and CLI can start
//! calling into it.

pub mod audit;
pub mod cost_model;
pub mod schema;

pub use audit::{PlanAudit, PlanKind, RING_BUFFER_CAP};
pub use cost_model::{CallSiteProfile, CostModel, CostModelUpdate, WelfordStats};
