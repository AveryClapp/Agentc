//! Replay the production exposure controller on predeclared engineering traces.
//! No provider calls, task labels, or alternative controller enter this binary.

use std::error::Error;
use std::fs::{self, OpenOptions};
use std::io::{BufWriter, Write};
use std::path::Path;

use agentc_optimizer::plan_guard::{PlanGuard, PlanGuardOutcome};
use agentc_optimizer::plan_profile::{
    CallSiteVersion, CallSiteVersionSpec, PlanProfileKey, PlanProfileUpdate, PlanProfiles,
    PlanRuntimeVersion,
};
use agentc_optimizer::ExecutionPlanId;
use serde::{Deserialize, Serialize};
use serde_json::json;

const START_US: i64 = 1_000_000;
const STEP_US: i64 = 1_000_000;
const WARM_PAIRS: u32 = 20;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Input {
    schema_version: u32,
    evidence_kind: String,
    divergence_mode: String,
    horizon: u32,
    repetitions: u32,
    seed: u64,
    rates: Vec<f64>,
    thresholds: Vec<f64>,
    scenarios: Vec<Scenario>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Scenario {
    scenario_id: String,
    family: String,
    execution_plan_id: ExecutionPlanId,
    runtime_version: PlanRuntimeVersion,
    divergence_before: f64,
    divergence_after: f64,
    change_at_call: u32,
    reference_error_every: Option<u32>,
}

#[derive(Serialize)]
struct Sequence {
    record_type: &'static str,
    scenario_id: String,
    family: String,
    repetition: u32,
    rate: f64,
    threshold: f64,
    rng_seed: u64,
    key: PlanProfileKey,
    warm_paired_observations: u32,
    warm_divergence_upper_p95: Option<f64>,
    executed_candidate_calls: u32,
    remaining_reference_calls: u32,
    sampled_calls: Vec<u32>,
    failed_reference_calls: Vec<u32>,
    // (call index, actual divergence, native exposure after accepting feedback)
    feedback: Vec<(u32, f64, f64)>,
    disabled_at_call: Option<u32>,
    disabled_at_us: Option<i64>,
    final_blocks_user_visible: bool,
    // This binary measures controller decisions, not disk persistence or FFI.
    durable_disable_measured: bool,
}

fn draw(state: &mut u64) -> f64 {
    // SplitMix64; the Python verifier independently reconstructs every draw.
    *state = state.wrapping_add(0x9e3779b97f4a7c15);
    let mut value = *state;
    value = (value ^ (value >> 30)).wrapping_mul(0xbf58476d1ce4e5b9);
    value = (value ^ (value >> 27)).wrapping_mul(0x94d049bb133111eb);
    value ^= value >> 31;
    (value >> 11) as f64 / (1_u64 << 53) as f64
}

fn update(key: &PlanProfileKey, runtime: &PlanRuntimeVersion, now_us: i64) -> PlanProfileUpdate {
    PlanProfileUpdate {
        cost_evidence: agentc_optimizer::cost_evidence::CostEvidence {
            source: agentc_optimizer::cost_evidence::CostSource::Estimated,
            physical_invocations: Vec::new(),
        },
        key: key.clone(),
        runtime_version: runtime.clone(),
        input_tokens: 100,
        output_tokens: 20,
        latency_ms: 25.0,
        cost_usd: 0.001,
        output_is_structured: false,
        output_is_short: true,
        divergence: None,
        dispatch_fallback: false,
        observed_at_us: Some(now_us),
    }
}

fn replay(
    input: &Input,
    scenario: &Scenario,
    ordinal: u64,
    repetition: u32,
    rate: f64,
    threshold: f64,
) -> Result<Sequence, Box<dyn Error>> {
    let version = CallSiteVersion::from_spec(&CallSiteVersionSpec {
        call_site_id: format!("guard-replay:{}", scenario.scenario_id),
        prompt_shape_version: "guard-arithmetic-fixture-v1".into(),
        provider_protocol: scenario.runtime_version.provider_protocol.clone(),
        tool_schema_version: None,
        application_config_version: Some(format!(
            "engineered-replay:{}:{}:{rate}:{threshold}",
            scenario.family, input.divergence_mode
        )),
    })?;
    let key = PlanProfileKey {
        call_site_version: version,
        execution_plan_id: scenario.execution_plan_id.clone(),
    };
    let profiles = PlanProfiles::new();
    let guard = PlanGuard::new();
    for warm in 0..WARM_PAIRS {
        let now = START_US - i64::from(WARM_PAIRS - warm);
        let token = profiles.observe(update(&key, &scenario.runtime_version, now))?;
        profiles.record_divergence_once(&token, 0.0, Some(now))?;
        guard.record_sample(&token, 0.0, threshold, Some(now))?;
    }
    let warm = profiles
        .get_for_reporting(&key)
        .ok_or("missing warm profile")?;
    let rng_seed = input
        .seed
        .wrapping_add(ordinal.wrapping_mul(1_000_003))
        .wrapping_add(u64::from(repetition));
    let mut state = rng_seed;
    let mut result = Sequence {
        record_type: "native_guard_replay_sequence",
        scenario_id: scenario.scenario_id.clone(),
        family: scenario.family.clone(),
        repetition,
        rate,
        threshold,
        rng_seed,
        key: key.clone(),
        warm_paired_observations: warm.paired_observations,
        warm_divergence_upper_p95: warm.divergence_upper_p95,
        executed_candidate_calls: 0,
        remaining_reference_calls: input.horizon,
        sampled_calls: Vec::new(),
        failed_reference_calls: Vec::new(),
        feedback: Vec::new(),
        disabled_at_call: None,
        disabled_at_us: None,
        final_blocks_user_visible: false,
        durable_disable_measured: false,
    };
    for call in 1..=input.horizon {
        let now = START_US + i64::from(call) * STEP_US;
        if guard.decision(&key, now).blocks_user_visible() {
            break;
        }
        result.executed_candidate_calls += 1;
        let token = profiles.observe(update(&key, &scenario.runtime_version, now))?;
        if draw(&mut state) >= rate {
            continue;
        }
        result.sampled_calls.push(call);
        if scenario
            .reference_error_every
            .is_some_and(|n| call % n == 0)
        {
            result.failed_reference_calls.push(call);
            continue;
        }
        let divergence = if call < scenario.change_at_call {
            scenario.divergence_before
        } else {
            scenario.divergence_after
        };
        profiles.record_divergence_once(&token, divergence, Some(now))?;
        let outcome = guard.record_sample(&token, divergence, threshold, Some(now))?;
        let exposure = guard
            .get(&key)
            .ok_or("missing native guard")?
            .divergence_exposure;
        result.feedback.push((call, divergence, exposure));
        if let PlanGuardOutcome::Disabled(disabled) = outcome {
            result.disabled_at_call = Some(call);
            result.disabled_at_us = Some(disabled.disabled_at_us);
        }
    }
    result.remaining_reference_calls = input.horizon - result.executed_candidate_calls;
    result.final_blocks_user_visible = guard
        .decision(&key, START_US + i64::from(input.horizon) * STEP_US)
        .blocks_user_visible();
    Ok(result)
}

fn main() -> Result<(), Box<dyn Error>> {
    let args: Vec<String> = std::env::args().collect();
    if args.len() != 3 {
        return Err("usage: guard_frontier_replay INPUT_JSON FRESH_OUTPUT_DIRECTORY".into());
    }
    let input: Input = serde_json::from_slice(&fs::read(&args[1])?)?;
    if input.schema_version != 1
        || input.evidence_kind != "engineering_fixture"
        || !["lexical", "normalized"].contains(&input.divergence_mode.as_str())
        || !(1..=1_000).contains(&input.horizon)
        || input.repetitions == 0
        || input.scenarios.is_empty()
        || input.rates != [0.0, 0.01, 0.02, 0.05, 0.10, 1.0]
        || input.thresholds != [0.05, 0.10, 0.15, 0.20, 0.30, 0.50]
    {
        return Err("invalid predeclared engineering replay contract".into());
    }
    for s in &input.scenarios {
        if s.scenario_id.is_empty()
            || s.family.is_empty()
            || s.change_at_call == 0
            || ![s.divergence_before, s.divergence_after]
                .iter()
                .all(|d| d.is_finite() && (0.0..=1.0).contains(d))
            || s.reference_error_every == Some(0)
        {
            return Err("invalid engineered replay scenario".into());
        }
    }
    let output = Path::new(&args[2]);
    fs::create_dir(output)?;
    let file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(output.join("sequences.jsonl"))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(output, fs::Permissions::from_mode(0o700))?;
        file.set_permissions(fs::Permissions::from_mode(0o600))?;
    }
    let mut writer = BufWriter::new(file);
    let mut count = 0_u64;
    for threshold in &input.thresholds {
        for rate in &input.rates {
            for (ordinal, scenario) in input.scenarios.iter().enumerate() {
                for repetition in 0..input.repetitions {
                    serde_json::to_writer(
                        &mut writer,
                        &replay(
                            &input,
                            scenario,
                            ordinal as u64,
                            repetition,
                            *rate,
                            *threshold,
                        )?,
                    )?;
                    writer.write_all(b"\n")?;
                    count += 1;
                }
            }
            eprintln!("completed threshold={threshold} rate={rate} sequences={count}");
        }
    }
    writer.flush()?;
    writer.get_ref().sync_all()?;
    let mut completion = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(output.join("completion.json"))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        completion.set_permissions(fs::Permissions::from_mode(0o600))?;
    }
    serde_json::to_writer(
        &mut completion,
        &json!({"record_type":"native_guard_replay_completion", "sequences":count,
        "source_stage":"E0", "provider_calls":0, "durable_disable_measured":false,
        "controller":"production PlanGuard::record_sample", "warm_paired_observations":WARM_PAIRS}),
    )?;
    completion.sync_all()?;
    println!("{}", json!({"sequences":count, "provider_calls":0}));
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture(change: u32, divergence: f64) -> (Input, Scenario) {
        let input = Input {
            schema_version: 1,
            evidence_kind: "engineering_fixture".into(),
            divergence_mode: "lexical".into(),
            horizon: 100,
            repetitions: 1,
            seed: 7,
            rates: vec![0.0, 0.01, 0.02, 0.05, 0.10, 1.0],
            thresholds: vec![0.05, 0.10, 0.15, 0.20, 0.30, 0.50],
            scenarios: vec![],
        };
        let scenario = Scenario {
            scenario_id: "unit-controller-case".into(),
            family: "unit-complete-plan".into(),
            execution_plan_id: ExecutionPlanId::parse("a".repeat(64)).unwrap(),
            runtime_version: PlanRuntimeVersion {
                provider_protocol: "unit".into(),
                target_model_id: "unit".into(),
                target_model_version: "unit-v1".into(),
                price_table_version: "unit-modeled".into(),
            },
            divergence_before: 0.0,
            divergence_after: divergence,
            change_at_call: change,
            reference_error_every: None,
        };
        (input, scenario)
    }

    #[test]
    fn dense_high_divergence_disables_after_two_fresh_native_observations() {
        let (input, scenario) = fixture(1, 1.0);
        let row = replay(&input, &scenario, 0, 0, 1.0, 0.05).unwrap();
        assert_eq!(row.sampled_calls, vec![1, 2]);
        assert_eq!(row.executed_candidate_calls, 2);
        assert_eq!(row.disabled_at_call, Some(2));
        assert_eq!(row.feedback[1].2, 1.9);
        assert!(!row.durable_disable_measured);
    }

    #[test]
    fn no_feedback_cannot_disable_a_harmful_plan() {
        let (input, scenario) = fixture(1, 1.0);
        let row = replay(&input, &scenario, 0, 0, 0.0, 0.05).unwrap();
        assert_eq!(row.executed_candidate_calls, 100);
        assert!(row.sampled_calls.is_empty());
        assert_eq!(row.disabled_at_call, None);
    }

    #[test]
    fn semantic_change_keeps_the_same_plan_and_preexisting_benign_window() {
        let (input, scenario) = fixture(26, 1.0);
        let row = replay(&input, &scenario, 0, 0, 1.0, 0.05).unwrap();
        assert_eq!(row.warm_paired_observations, 20);
        assert_eq!(row.warm_divergence_upper_p95, Some(0.0));
        assert_eq!(row.disabled_at_call, Some(27));
    }

    #[test]
    fn below_threshold_divergence_does_not_disable_even_with_dense_sampling() {
        let (input, scenario) = fixture(1, 0.18);
        let row = replay(&input, &scenario, 0, 0, 1.0, 0.20).unwrap();
        assert_eq!(row.sampled_calls.len(), 100);
        assert_eq!(row.disabled_at_call, None);
        assert!(row.feedback.iter().all(|event| event.2 == 0.0));
    }
}
