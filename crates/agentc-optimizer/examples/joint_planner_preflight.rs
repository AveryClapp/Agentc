//! Zero-network preflight for live complete-plan enumeration and selection.

use std::error::Error;
use std::sync::Arc;
use std::time::Instant;

use agentc_optimizer::ffi::{attach_observation_context, optimize_profiled_plan};
use agentc_optimizer::{
    default_model_catalog, Budget, Call, CostModel, CostModelUpdate, DepSource,
    ModelDowngradeRule, Optimizer, OptimizerConfig, OutputBudgetRule, Parameters, Plan,
    PlanGuard, PlanProfileKey, PlanProfileUpdate, PlanProfiles, PlanRuntimeVersion, RewriteRule,
    OPENAI_CHAT_COMPLETIONS_PROTOCOL,
};
use serde::Deserialize;
use serde_json::json;

const NOW_US: i64 = 2_000_000;
const MEASURED_CALLS: usize = 20_000;
const WARMUP_CALLS: usize = 1_000;

#[derive(Deserialize)]
struct ObservationContext {
    key: PlanProfileKey,
    runtime_version: PlanRuntimeVersion,
}

fn main() -> Result<(), Box<dyn Error>> {
    let catalog = Arc::new(default_model_catalog()?);
    let cost_model = Arc::new(CostModel::new());
    for observed_at_us in 1..=50 {
        cost_model.observe(CostModelUpdate {
            call_site_id: "joint-preflight-site".to_string(),
            input_tokens: 1_000,
            output_tokens: 80,
            latency_ms: 1_000.0,
            cost_usd: 0.01,
            output_is_structured: false,
            output_is_short: true,
            now_us: Some(observed_at_us),
        });
    }
    let budget = Arc::new(Budget::new());
    let rules: Vec<Box<dyn RewriteRule>> = vec![
        Box::new(OutputBudgetRule::default()),
        Box::new(ModelDowngradeRule::from_catalog(
            catalog.clone(),
            budget.clone(),
        )),
    ];
    let optimizer = Optimizer::with_budget(
        cost_model,
        rules,
        OptimizerConfig {
            shadow_rate: 0.0,
            ..OptimizerConfig::default()
        },
        budget,
    );
    let call = call();
    let call_json = serde_json::to_string(&call)?;
    let candidates = optimizer.candidate_plans(&call, &catalog);
    let joint_plan = candidates
        .iter()
        .find(|plan| {
            matches!(
                plan,
                Plan::Composed { rules, call, .. }
                    if rules.iter().any(|rule| rule.rule == "OutputBudget")
                        && rules.iter().any(|rule| rule.rule == "ModelDowngrade")
                        && call.model == "gpt-4o-mini-2024-07-18"
            )
        })
        .ok_or("joint candidate was not enumerated")?;
    let reference = Plan::PassThrough;
    let reference_context = context_for(&optimizer, &catalog, &call_json, &reference)?;
    let joint_context = context_for(&optimizer, &catalog, &call_json, joint_plan)?;
    let profiles = PlanProfiles::new();
    for sequence in 0..20 {
        let observed_at_us = NOW_US - 20 + sequence;
        profiles.observe(PlanProfileUpdate {
            key: reference_context.key.clone(),
            runtime_version: reference_context.runtime_version.clone(),
            input_tokens: 1_000,
            output_tokens: 80,
            latency_ms: 1_000.0,
            cost_usd: 0.01,
            output_is_structured: false,
            output_is_short: true,
            divergence: None,
            dispatch_fallback: false,
            observed_at_us: Some(observed_at_us),
        })?;
        profiles.observe(PlanProfileUpdate {
            key: joint_context.key.clone(),
            runtime_version: joint_context.runtime_version.clone(),
            input_tokens: 1_000,
            output_tokens: 80,
            latency_ms: 400.0,
            cost_usd: 0.001,
            output_is_structured: false,
            output_is_short: true,
            divergence: Some(0.0),
            dispatch_fallback: false,
            observed_at_us: Some(observed_at_us),
        })?;
    }
    let guard = PlanGuard::new();

    for _ in 0..WARMUP_CALLS {
        let _ = optimize_profiled_plan(
            &optimizer,
            Some(&catalog),
            &profiles,
            &guard,
            &call_json,
            NOW_US,
        );
    }

    let mut elapsed_ns = Vec::with_capacity(MEASURED_CALLS);
    let mut selected_joint = 0usize;
    let mut safe_reference_fallbacks = 0usize;
    let mut unexpected_identities = 0usize;
    for _ in 0..MEASURED_CALLS {
        let started = Instant::now();
        let selected = optimize_profiled_plan(
            &optimizer,
            Some(&catalog),
            &profiles,
            &guard,
            &call_json,
            NOW_US,
        );
        elapsed_ns.push(started.elapsed().as_nanos() as u64);
        let plan: Plan = serde_json::from_str(&selected)?;
        if matches!(plan, Plan::Composed { .. }) {
            selected_joint += 1;
        }
        match decode_context(&selected) {
            Ok(selected_context) if selected_context.key == joint_context.key => {}
            Ok(selected_context) if selected_context.key == reference_context.key => {
                safe_reference_fallbacks += 1;
            }
            _ => unexpected_identities += 1,
        }
    }
    elapsed_ns.sort_unstable();

    println!(
        "{}",
        serde_json::to_string_pretty(&json!({
            "stage": "E0",
            "paper_evidence": false,
            "network_calls": 0,
            "scenario": "live-profiled-joint-planner-preflight",
            "build_profile": if cfg!(debug_assertions) { "debug" } else { "release" },
            "candidate_count_including_reference": candidates.len(),
            "paired_evidence_for_selected_plan": 20,
            "measured_calls": MEASURED_CALLS,
            "exact_joint_selections": selected_joint,
            "safe_reference_fallbacks": safe_reference_fallbacks,
            "unexpected_identities": unexpected_identities,
            "latency_us": {
                "p50": percentile_ns(&elapsed_ns, 50) as f64 / 1_000.0,
                "p95": percentile_ns(&elapsed_ns, 95) as f64 / 1_000.0,
                "p99": percentile_ns(&elapsed_ns, 99) as f64 / 1_000.0,
                "max": elapsed_ns.last().copied().unwrap_or(0) as f64 / 1_000.0,
            },
        }))?
    );
    Ok(())
}

fn context_for(
    optimizer: &Optimizer,
    catalog: &agentc_optimizer::ModelCatalog,
    call_json: &str,
    plan: &Plan,
) -> Result<ObservationContext, Box<dyn Error>> {
    let plan_json = serde_json::to_string(plan)?;
    let attached = attach_observation_context(
        Some(catalog),
        call_json,
        &plan_json,
        optimizer.divergence_threshold_for_plan(plan),
    );
    decode_context(&attached)
}

fn decode_context(plan_json: &str) -> Result<ObservationContext, Box<dyn Error>> {
    let value: serde_json::Value = serde_json::from_str(plan_json)?;
    let context = value
        .get("agentc_observation_context")
        .cloned()
        .ok_or("plan has no observation context")?;
    Ok(serde_json::from_value(context)?)
}

fn percentile_ns(sorted: &[u64], percentile: usize) -> u64 {
    let index = sorted
        .len()
        .saturating_mul(percentile)
        .saturating_add(99)
        / 100;
    sorted[index.saturating_sub(1).min(sorted.len().saturating_sub(1))]
}

fn call() -> Call {
    Call {
        call_site_id: "joint-preflight-site".to_string(),
        trace_id: [0; 16],
        span_id: [0; 8],
        model: "gpt-4o".to_string(),
        messages: Vec::new(),
        parameters: Parameters {
            max_output_tokens: Some(512),
            extra: json!({
                "agentc_route_context": {
                    "provider_protocol": OPENAI_CHAT_COMPLETIONS_PROTOCOL,
                    "provider_namespace": "openai",
                    "input_tokens_upper_bound": 1_000,
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
