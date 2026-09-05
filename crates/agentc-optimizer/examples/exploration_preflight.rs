//! Zero-network preflight for the bounded exploration controller.

use std::error::Error;

use agentc_optimizer::{
    schema::ensure_cost_model_schema, CallSiteVersion, CounterfactualFeedback,
    CounterfactualLabel, ExecutionPlanId, ExplorationCandidate, ExplorationController,
    ExplorationPolicy, ExplorationReason, PlanProfileKey,
};
use rusqlite::Connection;
use serde_json::json;

const START_US: i64 = 1_000_000;

fn main() -> Result<(), Box<dyn Error>> {
    let directory = tempfile::tempdir()?;
    let database_path = directory.path().join("cost_model.db");
    let call_site_version = CallSiteVersion::parse("1".repeat(64))?;
    let reference_plan_id = ExecutionPlanId::parse("f".repeat(64))?;
    let mut candidates = vec![
        candidate(&call_site_version, 'a')?,
        candidate(&call_site_version, 'b')?,
    ];
    let policy = ExplorationPolicy {
        seed: 20_260_903,
        max_calls_per_site: 4,
        max_concurrent_per_site: 1,
        evidence_target: 20,
        window_us: 1_000,
        lease_duration_us: 100,
        divergence_exposure_budget: 0.5,
        task_damage_budget: Some(0.5),
    };
    let controller = ExplorationController::with_policy(policy.clone())?;
    let mut conn = Connection::open(&database_path)?;
    ensure_cost_model_schema(&conn)?;

    let mut selected_plans = Vec::new();
    let mut all_returned_reference = true;
    let mut exposed_plan = None;
    for offset in 0..4 {
        let now_us = START_US + offset * 10;
        let decision = controller.decide_and_reserve(
            &mut conn,
            &call_site_version,
            &reference_plan_id,
            &candidates,
            now_us,
        );
        all_returned_reference &= decision.return_reference;
        let lease = decision
            .counterfactual
            .ok_or("preflight unexpectedly failed to reserve a candidate")?;
        selected_plans.push(lease.key.execution_plan_id.to_string());

        let harmful = offset == 0;
        if harmful {
            exposed_plan = Some(lease.key.execution_plan_id.clone());
        }
        controller.complete(
            &mut conn,
            &lease,
            &CounterfactualFeedback {
                divergence: if harmful { 0.7 } else { 0.05 },
                cost_usd: 0.01,
                latency_ms: 25.0,
                label: if harmful {
                    CounterfactualLabel::TaskQuality {
                        reference_quality: 1.0,
                        candidate_quality: 0.4,
                    }
                } else {
                    CounterfactualLabel::ObservationOnly
                },
            },
            now_us + 1,
        )?;

        if let Some(candidate) = candidates
            .iter_mut()
            .find(|candidate| candidate.key == lease.key)
        {
            candidate.paired_observations += 1;
        }
    }

    let cap_decision = controller.decide_and_reserve(
        &mut conn,
        &call_site_version,
        &reference_plan_id,
        &candidates,
        START_US + 50,
    );
    let before_restart = controller.snapshot(&conn, &call_site_version, START_US + 50)?;
    drop(conn);

    let mut restarted_conn = Connection::open(&database_path)?;
    let restarted = ExplorationController::with_policy(policy)?;
    let restart_decision = restarted.decide_and_reserve(
        &mut restarted_conn,
        &call_site_version,
        &reference_plan_id,
        &candidates,
        START_US + 60,
    );
    let after_restart = restarted.snapshot(
        &restarted_conn,
        &call_site_version,
        START_US + 60,
    )?;

    let exposed_plan = exposed_plan.ok_or("missing exposed plan")?;
    let exposed_plan_was_not_retried = selected_plans
        .iter()
        .skip(1)
        .all(|plan_id| plan_id != exposed_plan.as_str());
    let output = json!({
        "stage": "E0",
        "paper_evidence": false,
        "network_calls": 0,
        "scenario": "bounded-exploration-persistence-preflight",
        "seed": 20_260_903_u64,
        "policy": {
            "max_calls_per_site_24h_equivalent": 4,
            "max_concurrent_counterfactuals": 1,
            "divergence_exposure_budget": 0.5,
            "task_damage_budget": 0.5,
        },
        "selected_plan_ids": selected_plans,
        "all_user_visible_results_were_reference": all_returned_reference,
        "exposed_plan_was_not_retried": exposed_plan_was_not_retried,
        "cap_reason_before_restart": reason_name(&cap_decision.reason),
        "cap_reason_after_restart": reason_name(&restart_decision.reason),
        "before_restart": before_restart,
        "after_restart": after_restart,
        "checks": {
            "call_cap_enforced": cap_decision.reason == ExplorationReason::CallCapExhausted,
            "call_cap_persisted": restart_decision.reason == ExplorationReason::CallCapExhausted,
            "counterfactual_cost_accounted": (after_restart.counterfactual_cost_usd - 0.04).abs() < 1e-12,
            "divergence_and_task_damage_separate":
                (after_restart.divergence_exposure - 0.6).abs() < 1e-12
                && (after_restart.task_damage - 0.6).abs() < 1e-12,
        },
    });
    println!("{}", serde_json::to_string_pretty(&output)?);
    Ok(())
}

fn candidate(
    call_site_version: &CallSiteVersion,
    plan_digit: char,
) -> Result<ExplorationCandidate, Box<dyn Error>> {
    Ok(ExplorationCandidate {
        key: PlanProfileKey {
            call_site_version: call_site_version.clone(),
            execution_plan_id: ExecutionPlanId::parse(plan_digit.to_string().repeat(64))?,
        },
        paired_observations: 0,
        refresh_required: false,
        request_compatible: true,
        forbidden: false,
        disabled: false,
        divergence_threshold: 0.1,
        divergence_exposure: 0.0,
    })
}

fn reason_name(reason: &ExplorationReason) -> &'static str {
    match reason {
        ExplorationReason::CandidateReserved => "candidate_reserved",
        ExplorationReason::NoEligibleCandidate => "no_eligible_candidate",
        ExplorationReason::CallCapExhausted => "call_cap_exhausted",
        ExplorationReason::ConcurrencyCapReached => "concurrency_cap_reached",
        ExplorationReason::InvalidClock => "invalid_clock",
        ExplorationReason::PersistenceFailure => "persistence_failure",
    }
}
