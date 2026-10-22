use agentc_optimizer::cost_evidence::{CostEvidence, CostSource, PhysicalInvocation};
use agentc_optimizer::plan_profile::{
    CallSiteVersion, PlanProfileKey, PlanProfileUpdate, PlanProfiles, PlanRuntimeVersion,
};
use agentc_optimizer::schema::{ensure_cost_model_schema, COST_MODEL_SCHEMA};
use agentc_optimizer::ExecutionPlanId;
use rusqlite::Connection;

fn update(source: CostSource) -> PlanProfileUpdate {
    PlanProfileUpdate {
        cost_evidence: CostEvidence {
            source,
            physical_invocations: vec![PhysicalInvocation {
                batch_id: "a".repeat(32),
                invocation_id: "b".repeat(32),
            }],
        },
        key: PlanProfileKey {
            call_site_version: CallSiteVersion::parse("c".repeat(64)).unwrap(),
            execution_plan_id: ExecutionPlanId::parse("d".repeat(64)).unwrap(),
        },
        runtime_version: PlanRuntimeVersion {
            provider_protocol: "openai.chat.completions.v1".into(),
            target_model_id: "model".into(),
            target_model_version: "revision-v1".into(),
            price_table_version: "price-v1".into(),
        },
        input_tokens: 2,
        output_tokens: 1,
        latency_ms: 10.0,
        cost_usd: 0.01,
        output_is_structured: false,
        output_is_short: true,
        divergence: None,
        dispatch_fallback: false,
        observed_at_us: Some(1),
    }
}

#[test]
fn estimates_and_unknowns_dominate_every_combination_of_auxiliary_cost() {
    use CostSource::*;
    for left in [
        Unknown,
        Estimated,
        ProviderReported,
        ProviderBillingVerified,
        NoInference,
    ] {
        for right in [
            Unknown,
            Estimated,
            ProviderReported,
            ProviderBillingVerified,
            NoInference,
        ] {
            let combined = left.combine(right);
            assert_eq!(combined, right.combine(left));
            assert_eq!(
                combined.is_measured(),
                left.is_measured() && right.is_measured()
            );
            if left == Unknown || right == Unknown {
                assert_eq!(combined, Unknown);
            } else if left == Estimated || right == Estimated {
                assert_eq!(combined, Estimated);
            }
        }
    }
}

#[test]
fn mixed_windows_remain_unmeasured_until_the_last_estimate_expires() {
    let mut conn = Connection::open_in_memory().unwrap();
    ensure_cost_model_schema(&conn).unwrap();
    let profiles = PlanProfiles::with_window(2);
    let known = update(CostSource::ProviderReported);
    profiles.observe(known.clone()).unwrap();
    profiles.observe(update(CostSource::Estimated)).unwrap();
    profiles.flush_dirty(&mut conn).unwrap();
    let recovered = PlanProfiles::with_window(2);
    recovered.warm_from_db(&conn).unwrap();
    let profile = recovered.get_for_reporting(&known.key).unwrap();
    assert!(!profile.measured_cost_complete());
    assert_eq!(profile.cost_sources.provider_reported, 1);
    assert_eq!(profile.cost_sources.estimated, 1);
    assert_eq!(
        profile.retained_samples()[1].cost_evidence,
        update(CostSource::Estimated).cost_evidence
    );
    recovered.observe(known.clone()).unwrap();
    assert!(!recovered
        .get_for_reporting(&known.key)
        .unwrap()
        .measured_cost_complete());
    recovered.observe(known.clone()).unwrap();
    let profile = recovered.get_for_reporting(&known.key).unwrap();
    assert!(profile.measured_cost_complete());
    assert_eq!(profile.n_observations, 4);
    assert_eq!(profile.cost_sources.provider_reported, 2);
    assert_eq!(profile.cost_sources.estimated, 0);
    recovered.flush_dirty(&mut conn).unwrap();
    let summary: String = conn
        .query_row(
            "SELECT cost_sources_json FROM execution_plan_profile",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(
        serde_json::from_str::<serde_json::Value>(&summary).unwrap()["provider_reported"],
        2
    );
    let mut changed = known;
    changed.runtime_version.target_model_version = "revision-v2".into();
    changed.cost_evidence.source = CostSource::Unknown;
    recovered.observe(changed.clone()).unwrap();
    let profile = recovered.get_for_reporting(&changed.key).unwrap();
    assert_eq!(profile.n_observations, 5);
    assert_eq!(profile.cost_sources.unknown, 1);
    assert_eq!(profile.cost_sources.provider_reported, 0);
    assert!(!profile.measured_cost_complete());
}

#[test]
fn legacy_migration_keeps_metrics_and_counts_but_never_invents_billing_provenance() {
    let mut conn = Connection::open_in_memory().unwrap();
    ensure_cost_model_schema(&conn).unwrap();
    let profiles = PlanProfiles::new();
    let original = update(CostSource::ProviderReported);
    profiles.observe(original.clone()).unwrap();
    profiles.flush_dirty(&mut conn).unwrap();
    conn.execute_batch("ALTER TABLE execution_plan_observation DROP COLUMN cost_evidence_json; ALTER TABLE execution_plan_profile DROP COLUMN cost_sources_json;").unwrap();
    conn.execute_batch(COST_MODEL_SCHEMA).unwrap();
    ensure_cost_model_schema(&conn).unwrap();
    ensure_cost_model_schema(&conn).unwrap();
    let recovered = PlanProfiles::new();
    recovered.warm_from_db(&conn).unwrap();
    let profile = recovered.get_for_reporting(&original.key).unwrap();
    assert_eq!(profile.n_observations, 1);
    assert_eq!(profile.cost_usd.mean, original.cost_usd);
    assert_eq!(profile.cost_sources.unknown, 1);
    assert_eq!(
        profile.retained_samples()[0].cost_evidence,
        CostEvidence::default()
    );
    assert!(!profile.measured_cost_complete());
}

#[test]
fn malformed_or_duplicate_physical_identities_never_mutate_the_profile() {
    for kind in ["duplicate", "foreign-shape", "no-inference"] {
        let profiles = PlanProfiles::new();
        let mut invalid = update(CostSource::ProviderReported);
        match kind {
            "duplicate" => invalid
                .cost_evidence
                .physical_invocations
                .push(invalid.cost_evidence.physical_invocations[0].clone()),
            "foreign-shape" => {
                invalid.cost_evidence.physical_invocations[0].batch_id = "not-a-receipt".into()
            }
            _ => {
                invalid.cost_evidence.source = CostSource::NoInference;
                invalid.cost_usd = 0.0;
            }
        }
        assert!(profiles.observe(invalid).is_err());
        assert!(profiles.is_empty());
        assert_eq!(profiles.dirty_len(), 0);
    }
}
