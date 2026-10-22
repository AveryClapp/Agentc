//! Frozen request policies for the static and sequential evaluation controls.
//!
//! Decisions use only the current request and a sealed configuration. No cost
//! or complete-plan interaction profiles are consulted. The FFI still applies
//! the complete-plan guard and issues the usual native observation identity.

use std::collections::{BTreeMap, HashSet};

use agentc_core::storage::content_hash;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use crate::cost_model::CallSiteProfile;
use crate::dag::Call;
use crate::model_catalog::{ModelCatalog, RequestRequirements, ROUTE_CONTEXT_KEY};
use crate::planner::{CostDriver, Plan, RewriteRule, RuleApplication};
use crate::rules::{ContextCompressRule, StateDropRule};

pub const VERSION: &str = "frozen-request-policy-v1";
pub const CONTEXT_KEY: &str = "frozen_request_policy";
pub const REWRITE_PARAMETERS_KEY: &str = "agentc_frozen_rewrite_parameters";

#[derive(Debug, Clone, Copy, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Mode {
    BestStaticJoint,
    RouteThenRewrite,
    RewriteThenRoute,
}

impl Mode {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::BestStaticJoint => "best_static_joint",
            Self::RouteThenRewrite => "route_then_rewrite",
            Self::RewriteThenRoute => "rewrite_then_route",
        }
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Declaration {
    schema_version: u16,
    policy_version: String,
    mode: Mode,
    calibration_lock_sha256: String,
    configuration_sha256: String,
    configuration_json: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Configuration {
    strong_model: String,
    cheap_model: String,
    divergence_threshold: f64,
    static_plan: Option<StaticPlan>,
    router: Option<Router>,
    rewrite_by_target: BTreeMap<String, Vec<Step>>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct StaticPlan {
    target_model: String,
    rewrites: Vec<Step>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Router {
    feature: String,
    cheap_max_prompt_bytes: u64,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(tag = "rule", deny_unknown_fields)]
enum Step {
    OutputBudget {
        max_output_tokens: u32,
    },
    ContextCompress {
        min_prompt_bytes: u32,
        min_dead_fraction: f32,
        dead_attention_epsilon: f64,
    },
    StateDrop,
}

impl Step {
    fn name(&self) -> &'static str {
        match self {
            Self::OutputBudget { .. } => "OutputBudget",
            Self::ContextCompress { .. } => "ContextCompress",
            Self::StateDrop => "StateDrop",
        }
    }

    fn valid(&self) -> bool {
        match self {
            Self::OutputBudget { max_output_tokens } => (64..=2048).contains(max_output_tokens),
            Self::ContextCompress {
                min_dead_fraction,
                dead_attention_epsilon,
                ..
            } => {
                min_dead_fraction.is_finite()
                    && (0.0..=1.0).contains(min_dead_fraction)
                    && dead_attention_epsilon.is_finite()
                    && (0.0..=1.0).contains(dead_attention_epsilon)
            }
            Self::StateDrop => true,
        }
    }
}

pub struct CompiledPolicy {
    pub plan: Plan,
    pub divergence_threshold: f64,
    pub decision: Value,
}

fn valid_digest(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

fn valid_steps(steps: &[Step]) -> bool {
    steps.len() <= 3
        && steps.iter().all(Step::valid)
        && steps.iter().map(Step::name).collect::<HashSet<_>>().len() == steps.len()
}

/// The router's feature is the actual rewritten/original message text, measured
/// in UTF-8 bytes. It is deliberately not called a provider token count.
fn prompt_bytes(call: &Call) -> u64 {
    call.messages.iter().map(|m| m.content.len() as u64).sum()
}

fn route<'a>(configuration: &'a Configuration, call: &Call) -> &'a str {
    if prompt_bytes(call)
        <= configuration
            .router
            .as_ref()
            .expect("validated router")
            .cheap_max_prompt_bytes
    {
        &configuration.cheap_model
    } else {
        &configuration.strong_model
    }
}

fn apply_steps(call: &mut Call, steps: &[Step], rules: &mut Vec<RuleApplication>) -> Vec<Value> {
    let mut results = Vec::new();
    for step in steps {
        let mut changed = false;
        match step {
            Step::OutputBudget { max_output_tokens } => {
                if call
                    .parameters
                    .max_output_tokens
                    .is_none_or(|cap| cap > *max_output_tokens)
                {
                    call.parameters.max_output_tokens = Some(*max_output_tokens);
                    changed = true;
                }
            }
            Step::ContextCompress {
                min_prompt_bytes,
                min_dead_fraction,
                dead_attention_epsilon,
            } => {
                if !call.has_opaque_native_messages() {
                    let mut input = call.clone();
                    input.parameters.extra["dead_attention_epsilon"] =
                        json!(dead_attention_epsilon);
                    let rule =
                        ContextCompressRule::new(*min_prompt_bytes as usize, *min_dead_fraction);
                    let profile = CallSiteProfile::new(&call.call_site_id);
                    if rule.applies(&input, &profile) {
                        if let Some(proposal) = rule.propose(&input, &profile) {
                            if let Plan::Rewritten {
                                call: rewritten, ..
                            } = &proposal.rewritten
                            {
                                if (proposal.safety_check)(&input) {
                                    *call = rewritten.clone();
                                    changed = true;
                                }
                            }
                        }
                    }
                }
            }
            Step::StateDrop => {
                if !call.has_opaque_native_messages() {
                    let rule = StateDropRule::default();
                    let profile = CallSiteProfile::new(&call.call_site_id);
                    if rule.applies(call, &profile) {
                        if let Some(proposal) = rule.propose(call, &profile) {
                            if let Plan::Rewritten {
                                call: rewritten, ..
                            } = &proposal.rewritten
                            {
                                if (proposal.safety_check)(call) {
                                    *call = rewritten.clone();
                                    changed = true;
                                }
                            }
                        }
                    }
                }
            }
        }
        if changed {
            call.parameters.extra[REWRITE_PARAMETERS_KEY][step.name()] =
                serde_json::to_value(step).expect("finite step");
            rules.push(RuleApplication {
                rule: step.name().to_string(),
                projected_savings_usd: 0.0,
                cost_driver: if matches!(step, Step::OutputBudget { .. }) {
                    CostDriver::OutputTokens
                } else {
                    CostDriver::InputTokens
                },
            });
        }
        results.push(json!({"rule": step.name(), "applied": changed}));
    }
    results
}

/// Compile one frozen control. Capability admission uses the final request;
/// an inapplicable rewrite stays visible without changing the frozen policy.
pub fn compile(
    catalog: &ModelCatalog,
    original: &Call,
    mode: Mode,
) -> Result<CompiledPolicy, &'static str> {
    let declaration: Declaration =
        serde_json::from_value(original.parameters.extra[ROUTE_CONTEXT_KEY][CONTEXT_KEY].clone())
            .map_err(|_| "invalid_frozen_policy")?;
    if declaration.schema_version != 1
        || declaration.policy_version != VERSION
        || declaration.mode != mode
        || !valid_digest(&declaration.calibration_lock_sha256)
        || !valid_digest(&declaration.configuration_sha256)
        || content_hash(&declaration.configuration_json) != declaration.configuration_sha256
    {
        return Err("invalid_frozen_policy");
    }
    let config: Configuration = serde_json::from_str(&declaration.configuration_json)
        .map_err(|_| "invalid_frozen_configuration")?;
    if original.model != config.strong_model
        || config.strong_model == config.cheap_model
        || !config.divergence_threshold.is_finite()
        || ![0.05, 0.10, 0.15, 0.20, 0.30, 0.50].contains(&config.divergence_threshold)
    {
        return Err("invalid_frozen_configuration");
    }
    let models = [&config.strong_model, &config.cheap_model];
    match mode {
        Mode::BestStaticJoint => {
            let plan = config.static_plan.as_ref().ok_or("missing_static_plan")?;
            if config.router.is_some()
                || !config.rewrite_by_target.is_empty()
                || !models.contains(&&plan.target_model)
                || !valid_steps(&plan.rewrites)
            {
                return Err("invalid_static_plan");
            }
        }
        _ => {
            let router = config.router.as_ref().ok_or("missing_router")?;
            if config.static_plan.is_some()
                || router.feature != "prompt_utf8_bytes_v1"
                || config.rewrite_by_target.len() != 2
                || models.iter().any(|m| {
                    config
                        .rewrite_by_target
                        .get(*m)
                        .is_none_or(|s| !valid_steps(s))
                })
            {
                return Err("invalid_sequential_policy");
            }
        }
    }
    let mut call = original.clone();
    let mut rules = Vec::new();
    let (target, router_bytes, rewrite_decisions) = match mode {
        Mode::BestStaticJoint => {
            let plan = config.static_plan.as_ref().expect("validated static plan");
            let decisions = apply_steps(&mut call, &plan.rewrites, &mut rules);
            (plan.target_model.as_str(), None, decisions)
        }
        Mode::RouteThenRewrite => {
            let target = route(&config, &call);
            let bytes = prompt_bytes(&call);
            let decisions = apply_steps(&mut call, &config.rewrite_by_target[target], &mut rules);
            (target, Some(bytes), decisions)
        }
        Mode::RewriteThenRoute => {
            let decisions = apply_steps(
                &mut call,
                &config.rewrite_by_target[&config.strong_model],
                &mut rules,
            );
            (route(&config, &call), Some(prompt_bytes(&call)), decisions)
        }
    };
    let requirements = RequestRequirements::from_call(&call).ok_or("missing_route_contract")?;
    let target_entry = catalog
        .resolve(
            &requirements.provider_protocol,
            &requirements.provider_namespace,
            target,
        )
        .ok_or("unknown_frozen_target")?;
    // Keep the requested source in the contract. Do not forge a route from an
    // intermediate target or bypass capabilities for a calibrated recipe.
    let metadata = catalog
        .routed_target(&call, target_entry)
        .map_err(|_| "incompatible_frozen_target")?;
    if target != original.model {
        metadata
            .annotate_call(&mut call)
            .map_err(|_| "invalid_route_metadata")?;
        call.model = target.to_string();
        let application = RuleApplication {
            rule: "ModelDowngrade".into(),
            projected_savings_usd: 0.0,
            cost_driver: CostDriver::ModelPrice,
        };
        if mode == Mode::RewriteThenRoute {
            rules.push(application);
        } else {
            rules.insert(0, application);
        }
    }
    let decision = json!({
        "policy_version": VERSION, "calibration_lock_sha256": declaration.calibration_lock_sha256,
        "configuration_sha256": declaration.configuration_sha256, "router_feature": "prompt_utf8_bytes_v1",
        "router_prompt_bytes": router_bytes, "target_model": target,
        "rewrite_decisions": rewrite_decisions, "selection_source": "frozen_configuration",
        "interaction_profiles_consulted": false, "cost_estimate_available": false,
    });
    let plan = match rules.len() {
        0 => Plan::PassThrough,
        1 => Plan::Rewritten {
            rule: rules[0].rule.clone(),
            call,
            projected_savings_usd: 0.0,
        },
        _ => Plan::Composed {
            rules,
            call,
            net_savings_usd: 0.0,
        },
    };
    Ok(CompiledPolicy {
        plan,
        divergence_threshold: config.divergence_threshold,
        decision,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::dag::{DepSource, Message, Parameters};
    use crate::model_catalog::default_model_catalog;

    const STRONG: &str = "openai/gpt-4o-2024-11-20";
    const CHEAP: &str = "openai/gpt-4o-mini-2024-07-18";

    fn configuration(mode: Mode) -> Value {
        let mut value = json!({
            "strong_model": STRONG, "cheap_model": CHEAP,
            "divergence_threshold": 0.05, "static_plan": null, "router": null,
            "rewrite_by_target": {},
        });
        if mode == Mode::BestStaticJoint {
            value["static_plan"] = json!({"target_model": CHEAP, "rewrites": [
                {"rule": "OutputBudget", "max_output_tokens": 128}
            ]});
        } else {
            value["router"] =
                json!({"feature": "prompt_utf8_bytes_v1", "cheap_max_prompt_bytes": 400});
            value["rewrite_by_target"] = json!({
                STRONG: [{"rule": "StateDrop"}, {"rule": "OutputBudget", "max_output_tokens": 384}],
                CHEAP: [{"rule": "OutputBudget", "max_output_tokens": 128}],
            });
        }
        value
    }

    fn call(mode: Mode, configuration: Value) -> Call {
        let configuration_json = configuration.to_string();
        let roles = ["system", "user", "tool", "assistant", "tool", "user"];
        let messages = roles
            .iter()
            .enumerate()
            .map(|(i, role)| Message {
                role: role.to_string(),
                content: if i == 2 {
                    "unused state ".repeat(100)
                } else {
                    format!("preserve {i}")
                },
            })
            .collect();
        let deps = [
            DepSource::Literal,
            DepSource::Literal,
            DepSource::State {
                key: "unused".into(),
            },
            DepSource::Literal,
            DepSource::Literal,
            DepSource::Literal,
        ];
        Call {
            call_site_id: "frozen-policy-control".into(),
            trace_id: [0; 16],
            span_id: [0; 8],
            model: STRONG.into(),
            messages,
            parameters: Parameters {
                max_output_tokens: Some(1024),
                extra: json!({
                    ROUTE_CONTEXT_KEY: {
                        "provider_protocol": "litellm.completion.v1", "provider_namespace": "openai",
                        "input_tokens_upper_bound": 1024, "image_input": false, "tool_calling": false,
                        "structured_outputs": false, "streaming": false,
                        CONTEXT_KEY: {"schema_version": 1, "policy_version": VERSION,
                            "mode": mode, "calibration_lock_sha256": "a".repeat(64),
                            "configuration_sha256": content_hash(&configuration_json), "configuration_json": configuration_json}
                    }, "message_deps": deps, "window_state_reads": []
                }),
                ..Parameters::default()
            },
            tools: Vec::new(),
            input_deps: vec![DepSource::Literal],
            occurrence_ix: 0,
        }
    }

    fn selected(plan: &Plan) -> &Call {
        match plan {
            Plan::Rewritten { call, .. } | Plan::Composed { call, .. } => call,
            _ => panic!("expected alternative"),
        }
    }

    #[test]
    fn sequential_router_uses_the_request_at_its_actual_stage() {
        let catalog = default_model_catalog().unwrap();
        let original = call(
            Mode::RouteThenRewrite,
            configuration(Mode::RouteThenRewrite),
        );
        let first = compile(&catalog, &original, Mode::RouteThenRewrite).unwrap();
        assert_eq!(selected(&first.plan).model, STRONG);
        assert_eq!(
            selected(&first.plan).parameters.max_output_tokens,
            Some(384)
        );
        let second_call = call(
            Mode::RewriteThenRoute,
            configuration(Mode::RewriteThenRoute),
        );
        let second = compile(&catalog, &second_call, Mode::RewriteThenRoute).unwrap();
        assert_eq!(selected(&second.plan).model, CHEAP);
        assert_eq!(
            selected(&second.plan).parameters.max_output_tokens,
            Some(384)
        );
        assert!(first.decision["router_prompt_bytes"].as_u64().unwrap() > 400);
        assert!(second.decision["router_prompt_bytes"].as_u64().unwrap() < 400);
        assert_eq!(original.messages.len(), 6);
        assert_eq!(selected(&first.plan).messages.len(), 5);
        assert_eq!(
            second.plan.rule_names(),
            vec!["StateDrop", "OutputBudget", "ModelDowngrade"]
        );
    }

    #[test]
    fn static_plan_uses_frozen_target_and_cap_without_live_profiles() {
        let catalog = default_model_catalog().unwrap();
        let input = call(Mode::BestStaticJoint, configuration(Mode::BestStaticJoint));
        let result = compile(&catalog, &input, Mode::BestStaticJoint).unwrap();
        assert_eq!(selected(&result.plan).model, CHEAP);
        assert_eq!(
            selected(&result.plan).parameters.max_output_tokens,
            Some(128)
        );
        assert_eq!(result.decision["interaction_profiles_consulted"], false);
        assert_eq!(result.decision["router_prompt_bytes"], Value::Null);
    }

    #[test]
    fn frozen_configuration_rejects_corruption_unregistered_targets_and_features() {
        let catalog = default_model_catalog().unwrap();
        let mut input = call(Mode::BestStaticJoint, configuration(Mode::BestStaticJoint));
        input.parameters.extra[ROUTE_CONTEXT_KEY][CONTEXT_KEY]["configuration_sha256"] =
            json!("b".repeat(64));
        assert_eq!(
            compile(&catalog, &input, Mode::BestStaticJoint).err(),
            Some("invalid_frozen_policy")
        );
        for change in [
            json!({"target_model": "unknown", "rewrites": []}),
            json!({"target_model": CHEAP, "rewrites": [{"rule": "OutputBudget", "max_output_tokens": 63}]}),
            json!({"target_model": CHEAP, "rewrites": [{"rule": "StateDrop"}, {"rule": "StateDrop"}]}),
        ] {
            let mut config = configuration(Mode::BestStaticJoint);
            config["static_plan"] = change;
            assert!(compile(
                &catalog,
                &call(Mode::BestStaticJoint, config),
                Mode::BestStaticJoint
            )
            .is_err());
        }
        let input = call(
            Mode::RouteThenRewrite,
            configuration(Mode::RouteThenRewrite),
        );
        assert_eq!(
            compile(&catalog, &input, Mode::RewriteThenRoute).err(),
            Some("invalid_frozen_policy")
        );
    }

    #[test]
    fn opaque_messages_keep_the_original_transcript_and_router_input() {
        let catalog = default_model_catalog().unwrap();
        let mut input = call(
            Mode::RewriteThenRoute,
            configuration(Mode::RewriteThenRoute),
        );
        input.parameters.extra[crate::dag::NATIVE_MESSAGES_OPAQUE_KEY] = json!(true);
        let result = compile(&catalog, &input, Mode::RewriteThenRoute).unwrap();
        assert_eq!(selected(&result.plan).model, STRONG);
        assert_eq!(selected(&result.plan).messages.len(), 6);
        assert_eq!(result.decision["rewrite_decisions"][0]["applied"], false);
    }

    #[test]
    fn ordered_input_rewrites_preserve_duplicate_message_provenance() {
        let catalog = default_model_catalog().unwrap();
        for order in [
            vec![
                json!({"rule": "StateDrop"}),
                json!({"rule": "ContextCompress", "min_prompt_bytes": 0, "min_dead_fraction": 0.1, "dead_attention_epsilon": 0.1}),
            ],
            vec![
                json!({"rule": "ContextCompress", "min_prompt_bytes": 0, "min_dead_fraction": 0.1, "dead_attention_epsilon": 0.1}),
                json!({"rule": "StateDrop"}),
            ],
        ] {
            let mut config = configuration(Mode::RewriteThenRoute);
            config["rewrite_by_target"][STRONG] = json!(order);
            let mut input = call(Mode::RewriteThenRoute, config);
            // Text equality cannot identify which duplicate was retained.
            input.messages[2].content = input.messages[4].content.clone();
            input.parameters.extra["attention_scores"] = json!([0.5, 1.0, 0.0, 0.3, 0.8, 1.0]);
            let result = compile(&catalog, &input, Mode::RewriteThenRoute).unwrap();
            let output = selected(&result.plan);
            assert_eq!(output.messages.len(), 5);
            assert_eq!(
                output.parameters.extra["attention_scores"],
                json!([0.5, 1.0, 0.3, 0.8, 1.0])
            );
            assert_eq!(
                output.parameters.extra["message_deps"]
                    .as_array()
                    .unwrap()
                    .len(),
                5
            );
            assert_eq!(
                output.parameters.extra["message_deps"][3],
                json!({"kind": "literal"})
            );
        }
    }

    #[test]
    fn utf8_feature_and_tighter_output_cap_are_preserved() {
        let catalog = default_model_catalog().unwrap();
        let mut input = call(
            Mode::RouteThenRewrite,
            configuration(Mode::RouteThenRewrite),
        );
        input.messages[2].content = "é".repeat(180);
        input.parameters.max_output_tokens = Some(64);
        let result = compile(&catalog, &input, Mode::RouteThenRewrite).unwrap();
        assert_eq!(result.decision["router_prompt_bytes"], 410);
        assert_eq!(
            selected(&result.plan).parameters.max_output_tokens,
            Some(64)
        );
    }
}
