//! Agentc JIT optimizer runtime.
//!
//! Shipped so far:
//! - O1 — empirical cost model + append-only audit table.
//! - O2 — `Optimizer::plan` entry point, `Plan` enum, cold-path and
//!   overhead kill switch, fail-open FFI wired via `agentc-profiler`.
//! - O3–O5 — the nine rewrite rules (`rules`), shadow-mode sampling
//!   (`shadow`), the accuracy-budget machine (`budget`), and default-on
//!   multi-rule composition (`composition`).
//! - Joint-planning foundation — canonical execution plans and bounded,
//!   versioned complete-plan profiles.

pub mod audit;
pub mod budget;
pub mod composition;
pub mod config;
pub mod cost_model;
pub mod dag;
pub mod dag_context;
pub mod execution_plan;
pub mod ffi;
pub mod model_catalog;
pub mod plan_guard;
pub mod plan_profile;
pub mod planner;
pub mod reporting;
pub mod rules;
pub mod schema;
pub mod shadow;
pub mod wiring;

pub use audit::{PlanAudit, PlanKind, RING_BUFFER_CAP};
pub use budget::{Budget, BudgetEntry, DisabledEntry, SampleOutcome, BREACH_STREAK, COOLDOWN_US};
pub use composition::{compose_proposals, CompositionResult};
pub use config::OptimizerConfig;
pub use cost_model::{CallSiteProfile, CostModel, CostModelUpdate, WelfordStats};
pub use dag::{Call, DepSource, Message, Outcome, Parameters, Tool};
pub use dag_context::{DagContextCache, DagSpan, DEFAULT_WINDOW, MAX_TRACES_CACHED};
pub use execution_plan::{
    select_candidate, CachePolicy as ExecutionCachePolicy, CandidatePlan, CandidateRejection,
    CandidateRejectionReason, ExecutionPlanId, ExecutionPlanSpec, PlanAdmission, PlanEstimate,
    PlanIdentityError, RewriteApplication, RewriteOrdering, Selection, SelectionObjective,
    SelectionPolicy, SelectionPolicyError, SelectionReason, ValidationPolicy,
    EXECUTION_PLAN_SCHEMA_VERSION,
};
pub use model_catalog::{
    default_model_catalog, CatalogError, ModelCapabilities, ModelCatalog, ModelPrice,
    ModelProvenance, ModelRevisionKind, ModelTarget, OutputTokenParameter, RequestRequirements,
    RoutedModelTarget, ANTHROPIC_MESSAGES_PROTOCOL, DEFAULT_MODEL_CATALOG_VERSION,
    DEFAULT_PRICE_TABLE_VERSION, LITELLM_COMPLETION_PROTOCOL, OPENAI_CHAT_COMPLETIONS_PROTOCOL,
    ROUTED_TARGET_KEY, ROUTE_CONTEXT_KEY,
};
pub use plan_guard::{
    PlanDisabledEntry, PlanExposureSample, PlanGuard, PlanGuardDecision, PlanGuardEntry,
    PlanGuardError, PlanGuardOutcome, DEFAULT_PLAN_EXPOSURE_BUDGET, PLAN_DISABLE_COOLDOWN_US,
    PLAN_EXPOSURE_WINDOW_US,
};
pub use plan_profile::{
    load_plan_profile, CallSiteVersion, CallSiteVersionError, CallSiteVersionSpec,
    PlanDivergenceSample, PlanObservationToken, PlanProfile, PlanProfileKey, PlanProfileSample,
    PlanProfileUpdate, PlanProfileUpdateError, PlanProfiles, PlanRuntimeVersion,
    CALL_SITE_VERSION_SCHEMA_VERSION, DEFAULT_PLAN_PROFILE_WINDOW,
};
pub use planner::{CostDriver, Optimizer, Plan, Proposal, RewriteRule, RuleApplication};
pub use reporting::{
    build_inspect, build_report, disable_rule, glob_to_sql_like, render_disable_summary,
    render_inspect, render_report, AccuracyStatus, CallSiteInspect, DisableSummary,
    OptimizerReport, RuleBreakdown, RuleFiringRate,
};
pub use rules::{
    is_known_rule_name, CacheHitRule, ContextCompressRule, DeadOutputTruncationRule,
    ModelDowngradeRoute, ModelDowngradeRule, OutputBudgetRule, ParallelBranchRule, PromptDedupRule,
    StateDropRule, StructuredTruncationRule, KNOWN_RULE_NAMES,
};
pub use wiring::{build_optimizer, Wired};
