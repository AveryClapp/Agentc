//! Vendor-free FFI surface.
//!
//! Pure-Rust `optimize_plan`/`optimize_observe` adapters that the PyO3
//! binding in `agentc-profiler::_native` re-exports. The adapters accept
//! JSON strings and — crucially — never panic on malformed input or
//! internal errors: every failure falls through to `{"kind":"pass_through"}`
//! so the caller always receives a valid [`crate::Plan`].
//!
//! Panic trapping lives HERE, inside [`optimize_plan`]'s own
//! `std::panic::catch_unwind`, so the fail-open guarantee (a panicking rule
//! becomes `PassThrough`, never an exception) is testable under `cargo test`
//! rather than only through the Python interpreter. The PyO3 binding keeps
//! its own outer `catch_unwind` as defence in depth at the actual boundary.

use std::sync::Arc;

use agentc_core::storage::{canonical_json, content_hash};
use serde::{Deserialize, Serialize};

use crate::cost_model::{CostModel, CostModelUpdate};
use crate::dag::{Call, DepSource, Outcome};
use crate::execution_plan::{
    CachePolicy, ExecutionPlanSpec, RewriteApplication, RewriteOrdering, ValidationPolicy,
    EXECUTION_PLAN_SCHEMA_VERSION,
};
use crate::model_catalog::{ModelCatalog, RequestRequirements, ROUTE_CONTEXT_KEY};
use crate::plan_profile::{
    CallSiteVersion, CallSiteVersionSpec, PlanObservationToken, PlanProfileKey, PlanProfileUpdate,
    PlanProfiles, PlanRuntimeVersion,
};
use crate::planner::{Optimizer, Plan};

/// Canonical PassThrough JSON, returned whenever anything goes sideways.
pub const PASS_THROUGH_JSON: &str = "{\"kind\":\"pass_through\"}";

const OBSERVATION_CONTEXT_KEY: &str = "agentc_observation_context";
const OBSERVATION_TOKEN_SCHEMA_VERSION: u16 = 1;
const PROMPT_SHAPE_SCHEMA_VERSION: u16 = 1;
const PLAN_IMPLEMENTATION_VERSION: &str = env!("CARGO_PKG_VERSION");

/// Identity and runtime metadata embedded in a returned Plan as an internal
/// JSON field. Python carries this field opaquely in ``Plan.raw_json``; it does
/// not need to understand or reconstruct any profile key.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
struct EmbeddedObservationContext {
    call_site_id: String,
    key: PlanProfileKey,
    runtime_version: PlanRuntimeVersion,
}

/// Opaque handle returned by ``optimize_observe`` and consumed by
/// ``optimize_record_divergence``. It binds delayed feedback to one exact
/// execution while retaining the legacy solo-rule attribution needed until the
/// plan-level guard replaces the compatibility guard.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
struct OpaqueObservationToken {
    schema_version: u16,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    plan_observation: Option<PlanObservationToken>,
    call_site_id: String,
    #[serde(default)]
    rules: Vec<String>,
}

/// Validated attribution returned to the PyO3 adapter after a divergence is
/// attached to its complete-plan profile.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DivergenceAttribution {
    pub call_site_id: String,
    pub solo_rule: Option<String>,
    /// False when the same token/value pair was replayed idempotently.
    pub newly_recorded: bool,
}

/// Plan a call. Any deserialization failure, internal error, **or panic**
/// (e.g. a rule that panics inside `propose`) yields `PASS_THROUGH_JSON`.
///
/// The `catch_unwind` IS the fail-open guarantee — keep it. Deleting it makes
/// `tests/fail_open.rs::rule_panic_is_converted_to_pass_through` panic.
pub fn optimize_plan(opt: &Optimizer, call_json: &str) -> String {
    std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        let call: Call = match serde_json::from_str(call_json) {
            Ok(c) => c,
            Err(_) => return PASS_THROUGH_JSON.to_string(),
        };
        let plan = opt.plan(&call);
        serde_json::to_string(&plan).unwrap_or_else(|_| PASS_THROUGH_JSON.to_string())
    }))
    .unwrap_or_else(|_| PASS_THROUGH_JSON.to_string())
}

/// Attach a self-contained, content-free profile identity to a planned call.
///
/// The field is deliberately embedded in the JSON returned to Python instead
/// of stored in a side table: concurrent calls and delayed observations cannot
/// cross-wire, and provider adapters only have to return the original JSON.
/// Unsupported calls (notably multi-call Parallel plans) remain executable but
/// receive no decision profile until their identity schema is defined.
pub fn attach_observation_context(
    catalog: Option<&ModelCatalog>,
    call_json: &str,
    plan_json: &str,
) -> String {
    let Some(catalog) = catalog else {
        return plan_json.to_string();
    };
    let Ok(original_call) = serde_json::from_str::<Call>(call_json) else {
        return plan_json.to_string();
    };
    let Ok(plan) = serde_json::from_str::<Plan>(plan_json) else {
        return plan_json.to_string();
    };
    let Some(context) = build_observation_context(catalog, &original_call, &plan) else {
        return plan_json.to_string();
    };
    let Ok(mut value) = serde_json::from_str::<serde_json::Value>(plan_json) else {
        return plan_json.to_string();
    };
    let Some(object) = value.as_object_mut() else {
        return plan_json.to_string();
    };
    let Ok(encoded) = serde_json::to_value(context) else {
        return plan_json.to_string();
    };
    object.insert(OBSERVATION_CONTEXT_KEY.to_string(), encoded);
    serde_json::to_string(&value).unwrap_or_else(|_| plan_json.to_string())
}

fn build_observation_context(
    catalog: &ModelCatalog,
    original_call: &Call,
    plan: &Plan,
) -> Option<EmbeddedObservationContext> {
    let requirements = RequestRequirements::from_call(original_call)?;
    let selected_call = selected_call(original_call, plan)?;
    let target = catalog.resolve(
        &requirements.provider_protocol,
        &requirements.provider_namespace,
        &selected_call.model,
    )?;
    let target_model_id = selected_call.model.clone();
    let target_model_version = if selected_call.model == target.model_id {
        target.model_version.clone()
    } else {
        // The adapter must preserve a reference request exactly. When that
        // request names a mutable alias, do not pretend it dispatched the
        // catalog's immutable snapshot; use an explicit observation cohort
        // until the provider response supplies a stronger revision token.
        format!("{}@{}", selected_call.model, catalog.catalog_version)
    };

    let prompt_shape_version = prompt_shape_version(original_call, &requirements);
    let tool_schema_version = tool_schema_version(original_call)?;
    let application_config_version = application_config_version(original_call);
    let call_site_version = CallSiteVersion::from_spec(&CallSiteVersionSpec {
        call_site_id: original_call.call_site_id.clone(),
        prompt_shape_version,
        provider_protocol: requirements.provider_protocol.clone(),
        tool_schema_version,
        application_config_version: Some(application_config_version),
    })
    .ok()?;

    let rules = plan_rules(plan);
    let rewrites = rules
        .iter()
        // Model routing is represented by target_model_id, not duplicated as
        // a semantic rewrite in the same identity.
        .filter(|rule| rule.as_str() != "ModelDowngrade")
        .map(|rule| RewriteApplication {
            stable_name: rule.clone(),
            implementation_version: PLAN_IMPLEMENTATION_VERSION.to_string(),
            parameters: rewrite_parameters(rule, original_call, selected_call),
        })
        .collect();
    let is_cached = matches!(plan, Plan::Cached { .. });
    let has_alternative = !rules.is_empty() || is_cached;
    let spec = ExecutionPlanSpec {
        schema_version: EXECUTION_PLAN_SCHEMA_VERSION,
        provider_protocol: requirements.provider_protocol.clone(),
        requested_model_id: original_call.model.clone(),
        target_model_id: target_model_id.clone(),
        target_model_revision: Some(target_model_version.clone()),
        rewrite_ordering: RewriteOrdering::Ordered,
        rewrites,
        cache_policy: CachePolicy {
            stable_name: if is_cached {
                "memoized-response"
            } else {
                "provider-call"
            }
            .to_string(),
            implementation_version: PLAN_IMPLEMENTATION_VERSION.to_string(),
            parameters: serde_json::json!({}),
        },
        output_budget: selected_call.parameters.max_output_tokens,
        validation_policy: ValidationPolicy {
            stable_name: if has_alternative {
                "sampled-reference"
            } else {
                "none"
            }
            .to_string(),
            implementation_version: "1".to_string(),
            parameters: if has_alternative {
                validation_policy_parameters()
            } else {
                serde_json::json!({})
            },
        },
    };
    let execution_plan_id = spec.execution_plan_id().ok()?;
    Some(EmbeddedObservationContext {
        call_site_id: original_call.call_site_id.clone(),
        key: PlanProfileKey {
            call_site_version,
            execution_plan_id,
        },
        runtime_version: PlanRuntimeVersion {
            provider_protocol: requirements.provider_protocol,
            target_model_id,
            target_model_version,
            price_table_version: catalog.price_table_version.clone(),
        },
    })
}

fn selected_call<'a>(original_call: &'a Call, plan: &'a Plan) -> Option<&'a Call> {
    match plan {
        Plan::PassThrough | Plan::Cached { .. } => Some(original_call),
        Plan::Rewritten { call, .. } | Plan::Composed { call, .. } => Some(call),
        // ExecutionPlanSpec currently represents one provider call. Assigning
        // a multi-call plan the identity of its first branch would pool unlike
        // executions, so abstain until Parallel gets an explicit schema.
        Plan::Parallel { .. } => None,
    }
}

fn plan_rules(plan: &Plan) -> Vec<String> {
    match plan {
        Plan::Rewritten { rule, .. } | Plan::Parallel { rule, .. } => vec![rule.clone()],
        Plan::Composed { rules, .. } => rules.iter().map(|rule| rule.rule.clone()).collect(),
        Plan::PassThrough | Plan::Cached { .. } => Vec::new(),
    }
}

fn prompt_shape_version(call: &Call, requirements: &RequestRequirements) -> String {
    let roles: Vec<&str> = call
        .messages
        .iter()
        .map(|message| message.role.trim())
        .collect();
    let dependency_kinds: Vec<&'static str> = call
        .input_deps
        .iter()
        .map(|dependency| match dependency {
            DepSource::Literal => "literal",
            DepSource::UserInput { .. } => "user_input",
            DepSource::ToolOutput { .. } => "tool_output",
            DepSource::LlmOutput { .. } => "llm_output",
            DepSource::State { .. } => "state",
        })
        .collect();
    let structured_output_schema_version = call
        .parameters
        .extra
        .as_object()
        .and_then(|extra| extra.get(ROUTE_CONTEXT_KEY))
        .and_then(serde_json::Value::as_object)
        .and_then(|context| context.get("structured_output_schema_version"))
        .and_then(serde_json::Value::as_str);
    let value = serde_json::json!({
        "schema_version": PROMPT_SHAPE_SCHEMA_VERSION,
        "roles": roles,
        "dependency_kinds": dependency_kinds,
        "message_count": call.messages.len(),
        "dependency_count": call.input_deps.len(),
        "native_messages_opaque": call.has_opaque_native_messages(),
        "image_input": requirements.image_input,
        "structured_outputs": requirements.structured_outputs,
        "structured_output_schema_version": structured_output_schema_version,
        "streaming": requirements.streaming,
    });
    format!(
        "prompt-shape-v{PROMPT_SHAPE_SCHEMA_VERSION}:{}",
        content_hash(&canonical_json(&value))
    )
}

fn tool_schema_version(call: &Call) -> Option<Option<String>> {
    if call.tools.is_empty() {
        return Some(None);
    }
    let value = serde_json::to_value(&call.tools).ok()?;
    Some(Some(format!(
        "tool-schema-v1:{}",
        content_hash(&canonical_json(&value))
    )))
}

fn application_config_version(call: &Call) -> String {
    let value = serde_json::json!({
        "schema_version": 1,
        "temperature": call.parameters.temperature,
        "top_p": call.parameters.top_p,
        "stop": call.parameters.stop,
    });
    format!(
        "application-config-v1:{}",
        content_hash(&canonical_json(&value))
    )
}

fn rewrite_parameters(rule: &str, original_call: &Call, selected_call: &Call) -> serde_json::Value {
    match rule {
        "ContextCompress" => original_call
            .parameters
            .extra
            .as_object()
            .and_then(|extra| extra.get("dead_attention_epsilon"))
            .map(|epsilon| serde_json::json!({"dead_attention_epsilon": epsilon}))
            .unwrap_or_else(|| serde_json::json!({})),
        "OutputBudget" | "DeadOutputTruncation" => serde_json::json!({
            "max_output_tokens": selected_call.parameters.max_output_tokens,
        }),
        _ => serde_json::json!({}),
    }
}

fn validation_policy_parameters() -> serde_json::Value {
    let rate = std::env::var("AGENTC_OPTIMIZE_SHADOW")
        .ok()
        .and_then(|value| value.trim().parse::<f64>().ok())
        .filter(|value| value.is_finite())
        .unwrap_or(0.02)
        .clamp(0.0, 1.0);
    let mode = std::env::var("AGENTC_SHADOW_DIVERGENCE_MODE")
        .unwrap_or_else(|_| "lexical".to_string())
        .trim()
        .to_ascii_lowercase();
    let mode = match mode.as_str() {
        "normalized" | "embedding" => mode,
        _ => "lexical".to_string(),
    };
    let divergence_threshold = std::env::var("AGENTC_SHADOW_DIVERGENCE_BUDGET")
        .ok()
        .and_then(|value| value.trim().parse::<f64>().ok())
        .filter(|value| value.is_finite() && (0.0..=1.0).contains(value))
        .map(|value| serde_json::json!({"source": "environment", "value": value}))
        // The firing rule is already part of the execution-plan identity, so
        // this symbolic source remains exact for stable rule defaults while
        // avoiding a made-up common threshold for composed plans.
        .unwrap_or_else(|| serde_json::json!({"source": "rule-default"}));
    serde_json::json!({
        "divergence_threshold": divergence_threshold,
        "divergence_mode": mode,
        "sampling_rate": rate,
    })
}

/// Fold the outcome of a dispatched plan into the cost model. Failures
/// are swallowed — the user's call already returned, so there's no way to
/// surface the error anyway.
pub fn optimize_observe(
    cost_model: &Arc<CostModel>,
    plan_profiles: &PlanProfiles,
    plan_json: &str,
    outcome_json: &str,
) -> Result<String, String> {
    let plan: Plan = serde_json::from_str(plan_json).map_err(|e| e.to_string())?;
    let outcome: Outcome = serde_json::from_str(outcome_json).map_err(|e| e.to_string())?;
    let observation_context = embedded_observation_context(plan_json)?;

    // Only Rewritten/Parallel/PassThrough actually carry a call worth
    // attributing; Cached is served from memoization's cache stats, not
    // the optimizer's cost model.
    let inferred_call_site_id = match &plan {
        Plan::Rewritten { call, .. } | Plan::Composed { call, .. } => call.call_site_id.clone(),
        Plan::Parallel { calls, .. } => calls
            .first()
            .map(|c| c.call_site_id.clone())
            .unwrap_or_default(),
        // For PassThrough / Cached the plan itself doesn't carry a Call,
        // so the caller must populate `outcome.call_site_id`. Without it
        // the cost model never warms up cold sites.
        Plan::PassThrough | Plan::Cached { .. } => outcome.call_site_id.clone().unwrap_or_default(),
    };
    if let Some(context) = observation_context.as_ref() {
        if !inferred_call_site_id.is_empty() && inferred_call_site_id != context.call_site_id {
            return Err("plan observation context is bound to a different call site".to_string());
        }
    }
    let call_site_id = observation_context
        .as_ref()
        .map(|context| context.call_site_id.clone())
        .unwrap_or(inferred_call_site_id);
    if !call_site_id.is_empty() {
        cost_model.observe(CostModelUpdate {
            call_site_id: call_site_id.clone(),
            input_tokens: outcome.input_tokens,
            output_tokens: outcome.output_tokens,
            latency_ms: outcome.latency_ms,
            cost_usd: outcome.cost_usd,
            output_is_structured: outcome.output_is_structured,
            output_is_short: outcome.output_is_short,
            now_us: None,
        });
    }

    // For composed plans, also record per-rule-set realized savings so the
    // cost model can track composition payoff vs. solo rules over time.
    if let Plan::Composed {
        rules,
        net_savings_usd,
        ..
    } = &plan
    {
        let rule_names: Vec<&str> = rules.iter().map(|r| r.rule.as_str()).collect();
        cost_model.observe_rule_set(&call_site_id, &rule_names, *net_savings_usd as f64);
    }

    let plan_observation = observation_context
        .map(|context| {
            let runtime_version = observed_runtime_version(&context.runtime_version, &outcome);
            plan_profiles.observe(PlanProfileUpdate {
                key: context.key,
                runtime_version,
                input_tokens: outcome.input_tokens,
                output_tokens: outcome.output_tokens,
                latency_ms: outcome.latency_ms,
                cost_usd: outcome.cost_usd,
                output_is_structured: outcome.output_is_structured,
                output_is_short: outcome.output_is_short,
                divergence: None,
                dispatch_fallback: outcome.dispatch_fallback,
                observed_at_us: None,
            })
        })
        .transpose()
        .map_err(|error| error.to_string())?;
    serde_json::to_string(&OpaqueObservationToken {
        schema_version: OBSERVATION_TOKEN_SCHEMA_VERSION,
        plan_observation,
        call_site_id,
        rules: plan_rules(&plan),
    })
    .map_err(|error| error.to_string())
}

fn observed_runtime_version(
    planned: &PlanRuntimeVersion,
    outcome: &Outcome,
) -> PlanRuntimeVersion {
    let mut observed = planned.clone();
    if !outcome.dispatch_fallback {
        if let Some(executed_model_id) = outcome
            .executed_model_id
            .as_deref()
            .map(str::trim)
            .filter(|value| !value.is_empty())
        {
            if executed_model_id != planned.target_model_id {
                observed.target_model_version = executed_model_id.to_string();
            }
        }
    }
    observed
}

fn embedded_observation_context(
    plan_json: &str,
) -> Result<Option<EmbeddedObservationContext>, String> {
    let value: serde_json::Value = serde_json::from_str(plan_json).map_err(|e| e.to_string())?;
    value
        .get(OBSERVATION_CONTEXT_KEY)
        .cloned()
        .map(serde_json::from_value)
        .transpose()
        .map_err(|error| error.to_string())
}

/// Attach a delayed reference comparison to the exact execution identified by
/// an opaque observation token. The returned solo-rule attribution lets the
/// PyO3 adapter maintain the legacy guard without charging a composed result
/// independently to every constituent rule.
pub fn record_observation_divergence(
    plan_profiles: &PlanProfiles,
    observation_token: &str,
    divergence: f64,
) -> Result<DivergenceAttribution, String> {
    let token: OpaqueObservationToken =
        serde_json::from_str(observation_token).map_err(|error| error.to_string())?;
    if token.schema_version != OBSERVATION_TOKEN_SCHEMA_VERSION {
        return Err("unsupported observation token schema version".to_string());
    }
    if !divergence.is_finite() || !(0.0..=1.0).contains(&divergence) {
        return Err("divergence must be finite and in [0, 1]".to_string());
    }
    let newly_recorded = token
        .plan_observation
        .as_ref()
        .map(|observation| plan_profiles.record_divergence_once(observation, divergence, None))
        .transpose()
        .map_err(|error| error.to_string())?
        .unwrap_or(true);
    let solo_rule = match token.rules.as_slice() {
        [rule] => Some(rule.clone()),
        _ => None,
    };
    Ok(DivergenceAttribution {
        call_site_id: token.call_site_id,
        solo_rule,
        newly_recorded,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::OptimizerConfig;
    use crate::dag::{Message, Parameters};
    use crate::model_catalog::{default_model_catalog, OPENAI_CHAT_COMPLETIONS_PROTOCOL};
    use crate::planner::CostDriver;

    fn empty_optimizer() -> Optimizer {
        Optimizer::empty(Arc::new(CostModel::new()), OptimizerConfig::default())
    }

    fn observable_call(site: &str, content: &str) -> Call {
        Call {
            call_site_id: site.to_string(),
            trace_id: [0; 16],
            span_id: [0; 8],
            model: "gpt-4o".to_string(),
            messages: vec![Message {
                role: "user".to_string(),
                content: content.to_string(),
            }],
            parameters: Parameters {
                max_output_tokens: Some(128),
                extra: serde_json::json!({
                    "agentc_route_context": {
                        "provider_protocol": OPENAI_CHAT_COMPLETIONS_PROTOCOL,
                        "provider_namespace": "openai",
                        "input_tokens_upper_bound": 64,
                        "image_input": false,
                        "tool_calling": false,
                        "structured_outputs": false,
                        "streaming": false,
                    }
                }),
                ..Parameters::default()
            },
            tools: Vec::new(),
            input_deps: vec![DepSource::Literal],
            occurrence_ix: 0,
        }
    }

    fn outcome(site: &str) -> Outcome {
        Outcome {
            input_tokens: 100,
            output_tokens: 50,
            latency_ms: 100.0,
            cost_usd: 0.001,
            output_is_structured: false,
            output_is_short: true,
            call_site_id: Some(site.to_string()),
            ..Outcome::default()
        }
    }

    #[test]
    fn malformed_call_json_yields_pass_through() {
        let s = optimize_plan(&empty_optimizer(), "not json");
        assert_eq!(s, PASS_THROUGH_JSON);
    }

    #[test]
    fn valid_call_cold_site_yields_pass_through() {
        let call = serde_json::json!({
            "call_site_id": "site-x",
            "trace_id": "00".repeat(16),
            "span_id": "00".repeat(8),
            "model": "gpt-4o",
            "messages": [],
        });
        let s = optimize_plan(&empty_optimizer(), &call.to_string());
        // Valid round-trip, but cold ⇒ still pass_through.
        let v: serde_json::Value = serde_json::from_str(&s).unwrap();
        assert_eq!(v["kind"], "pass_through");
    }

    #[test]
    fn observe_on_pass_through_without_site_is_noop() {
        let cm = Arc::new(CostModel::new());
        let profiles = PlanProfiles::new();
        let plan = Plan::PassThrough;
        let outcome = Outcome {
            input_tokens: 1,
            output_tokens: 1,
            latency_ms: 1.0,
            cost_usd: 0.001,
            output_is_structured: false,
            output_is_short: true,
            call_site_id: None,
            ..Outcome::default()
        };
        let ok = optimize_observe(
            &cm,
            &profiles,
            &serde_json::to_string(&plan).unwrap(),
            &serde_json::to_string(&outcome).unwrap(),
        );
        assert!(ok.is_ok());
        assert!(cm.get("anything").is_none());
    }

    #[test]
    fn observe_on_pass_through_with_site_updates_cost_model() {
        let cm = Arc::new(CostModel::new());
        let profiles = PlanProfiles::new();
        let plan = Plan::PassThrough;
        let outcome = Outcome {
            input_tokens: 100,
            output_tokens: 50,
            latency_ms: 100.0,
            cost_usd: 0.001,
            output_is_structured: false,
            output_is_short: true,
            call_site_id: Some("site-warm".to_string()),
            ..Outcome::default()
        };
        let ok = optimize_observe(
            &cm,
            &profiles,
            &serde_json::to_string(&plan).unwrap(),
            &serde_json::to_string(&outcome).unwrap(),
        );
        assert!(ok.is_ok());
        let prof = cm.get("site-warm").expect("site warmed");
        assert_eq!(prof.n_observations, 1);
    }

    #[test]
    fn observation_context_is_stable_across_prompt_values_of_the_same_shape() {
        let catalog = default_model_catalog().unwrap();
        let plan = serde_json::to_string(&Plan::PassThrough).unwrap();
        let first = observable_call("site", "first private value");
        let second = observable_call("site", "different private value");

        let first_json = attach_observation_context(
            Some(&catalog),
            &serde_json::to_string(&first).unwrap(),
            &plan,
        );
        let second_json = attach_observation_context(
            Some(&catalog),
            &serde_json::to_string(&second).unwrap(),
            &plan,
        );
        let first_context = embedded_observation_context(&first_json).unwrap().unwrap();
        let second_context = embedded_observation_context(&second_json).unwrap().unwrap();

        assert_eq!(first_context.key, second_context.key);
        assert!(!first_json.contains("first private value"));
        assert!(!second_json.contains("different private value"));
    }

    #[test]
    fn ordered_message_structure_changes_call_site_version() {
        let catalog = default_model_catalog().unwrap();
        let plan = serde_json::to_string(&Plan::PassThrough).unwrap();
        let first = observable_call("site", "private value");
        let mut second = first.clone();
        second.messages.insert(
            0,
            Message {
                role: "system".to_string(),
                content: "another private value".to_string(),
            },
        );
        second.input_deps.insert(0, DepSource::Literal);

        let first_json = attach_observation_context(
            Some(&catalog),
            &serde_json::to_string(&first).unwrap(),
            &plan,
        );
        let second_json = attach_observation_context(
            Some(&catalog),
            &serde_json::to_string(&second).unwrap(),
            &plan,
        );
        let first_context = embedded_observation_context(&first_json).unwrap().unwrap();
        let second_context = embedded_observation_context(&second_json).unwrap().unwrap();

        assert_ne!(
            first_context.key.call_site_version,
            second_context.key.call_site_version
        );
        assert!(!second_json.contains("another private value"));
    }

    #[test]
    fn structured_output_contract_changes_call_site_version() {
        let catalog = default_model_catalog().unwrap();
        let plan = serde_json::to_string(&Plan::PassThrough).unwrap();
        let mut first = observable_call("site", "private value");
        let mut second = first.clone();
        first.parameters.extra[ROUTE_CONTEXT_KEY]["structured_outputs"] =
            serde_json::Value::Bool(true);
        first.parameters.extra[ROUTE_CONTEXT_KEY]["structured_output_schema_version"] =
            serde_json::Value::String("structured-output-v1:first".to_string());
        second.parameters.extra[ROUTE_CONTEXT_KEY]["structured_outputs"] =
            serde_json::Value::Bool(true);
        second.parameters.extra[ROUTE_CONTEXT_KEY]["structured_output_schema_version"] =
            serde_json::Value::String("structured-output-v1:second".to_string());

        let first_json = attach_observation_context(
            Some(&catalog),
            &serde_json::to_string(&first).unwrap(),
            &plan,
        );
        let second_json = attach_observation_context(
            Some(&catalog),
            &serde_json::to_string(&second).unwrap(),
            &plan,
        );
        let first_context = embedded_observation_context(&first_json).unwrap().unwrap();
        let second_context = embedded_observation_context(&second_json).unwrap().unwrap();

        assert_ne!(
            first_context.key.call_site_version,
            second_context.key.call_site_version
        );
    }

    #[test]
    fn observe_returns_token_that_correlates_one_exact_plan_divergence() {
        let catalog = default_model_catalog().unwrap();
        let original = observable_call("profiled-site", "private value");
        let selected = original.clone();
        let plan = Plan::Rewritten {
            rule: "OutputBudget".to_string(),
            call: selected,
            projected_savings_usd: 0.01,
        };
        let observable_plan = attach_observation_context(
            Some(&catalog),
            &serde_json::to_string(&original).unwrap(),
            &serde_json::to_string(&plan).unwrap(),
        );
        let cost_model = Arc::new(CostModel::new());
        let profiles = PlanProfiles::new();

        let token_json = optimize_observe(
            &cost_model,
            &profiles,
            &observable_plan,
            &serde_json::to_string(&outcome("profiled-site")).unwrap(),
        )
        .unwrap();
        let token: OpaqueObservationToken = serde_json::from_str(&token_json).unwrap();
        let observation = token.plan_observation.as_ref().unwrap();
        let before = profiles.get_for_reporting(observation.key()).unwrap();
        assert_eq!(before.window_observations, 1);
        assert_eq!(before.paired_observations, 0);

        let first = record_observation_divergence(&profiles, &token_json, 0.1).unwrap();
        let replay = record_observation_divergence(&profiles, &token_json, 0.1).unwrap();
        let after = profiles.get_for_reporting(observation.key()).unwrap();

        assert_eq!(first.solo_rule.as_deref(), Some("OutputBudget"));
        assert!(first.newly_recorded);
        assert!(!replay.newly_recorded);
        assert_eq!(after.paired_observations, 1);
        assert_eq!(after.n_paired_observations, 1);
    }

    #[test]
    fn stale_runtime_binding_is_rejected() {
        let catalog = default_model_catalog().unwrap();
        let original = observable_call("stale-site", "private value");
        let plan = Plan::Rewritten {
            rule: "OutputBudget".to_string(),
            call: original.clone(),
            projected_savings_usd: 0.01,
        };
        let observable_plan = attach_observation_context(
            Some(&catalog),
            &serde_json::to_string(&original).unwrap(),
            &serde_json::to_string(&plan).unwrap(),
        );
        let profiles = PlanProfiles::new();
        let token = optimize_observe(
            &Arc::new(CostModel::new()),
            &profiles,
            &observable_plan,
            &serde_json::to_string(&outcome("stale-site")).unwrap(),
        )
        .unwrap();
        let mut tampered: serde_json::Value = serde_json::from_str(&token).unwrap();
        tampered["plan_observation"]["runtime_version"]["target_model_version"] =
            serde_json::Value::String("stale-model-version".to_string());

        let error =
            record_observation_divergence(&profiles, &tampered.to_string(), 0.1).unwrap_err();
        assert!(error.contains("no longer current"));
    }

    #[test]
    fn provider_reported_model_revision_versions_the_observation() {
        let catalog = default_model_catalog().unwrap();
        let original = observable_call("alias-site", "private value");
        let plan = serde_json::to_string(&Plan::PassThrough).unwrap();
        let observable_plan = attach_observation_context(
            Some(&catalog),
            &serde_json::to_string(&original).unwrap(),
            &plan,
        );
        let context = embedded_observation_context(&observable_plan)
            .unwrap()
            .unwrap();
        assert_eq!(context.runtime_version.target_model_id, "gpt-4o");
        assert_eq!(
            context.runtime_version.target_model_version,
            format!("gpt-4o@{}", catalog.catalog_version)
        );
        let mut observed_outcome = outcome("alias-site");
        observed_outcome.executed_model_id = Some("gpt-4o-provider-revision".to_string());

        let token = optimize_observe(
            &Arc::new(CostModel::new()),
            &PlanProfiles::new(),
            &observable_plan,
            &serde_json::to_string(&observed_outcome).unwrap(),
        )
        .unwrap();
        let token: OpaqueObservationToken = serde_json::from_str(&token).unwrap();
        let observation = token.plan_observation.unwrap();

        assert_eq!(
            observation.runtime_version().target_model_version,
            "gpt-4o-provider-revision"
        );
    }

    #[test]
    fn composed_token_has_no_fabricated_solo_rule_attribution() {
        let catalog = default_model_catalog().unwrap();
        let original = observable_call("composed-site", "private value");
        let plan = Plan::Composed {
            rules: vec![
                crate::planner::RuleApplication {
                    rule: "ContextCompress".to_string(),
                    projected_savings_usd: 0.01,
                    cost_driver: CostDriver::InputTokens,
                },
                crate::planner::RuleApplication {
                    rule: "OutputBudget".to_string(),
                    projected_savings_usd: 0.01,
                    cost_driver: CostDriver::OutputTokens,
                },
            ],
            call: original.clone(),
            net_savings_usd: 0.02,
        };
        let observable_plan = attach_observation_context(
            Some(&catalog),
            &serde_json::to_string(&original).unwrap(),
            &serde_json::to_string(&plan).unwrap(),
        );
        let profiles = PlanProfiles::new();
        let token = optimize_observe(
            &Arc::new(CostModel::new()),
            &profiles,
            &observable_plan,
            &serde_json::to_string(&outcome("composed-site")).unwrap(),
        )
        .unwrap();

        let attribution = record_observation_divergence(&profiles, &token, 0.2).unwrap();
        assert!(attribution.solo_rule.is_none());
        assert!(attribution.newly_recorded);
    }
}
