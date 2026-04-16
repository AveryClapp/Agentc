//! Cross-rule integration tests (O5 exit criteria).
//!
//! Unit tests live next to each rule; this file proves the planner
//! picks the right winner when multiple rules bid on the same call.

use std::sync::Arc;

use agentc_memo::{Cache, CacheHit, CacheKey, CacheSource, CacheStats, CacheValue};
use agentc_optimizer::{
    budget::Budget,
    config::OptimizerConfig,
    cost_model::{CostModel, CostModelUpdate, WelfordStats},
    dag::{Call, DepSource, Message, Parameters},
    planner::{Optimizer, Plan},
    rules::{
        cache_hit::CacheKeyBuilder, CacheHitRule, ModelDowngradeRoute, ModelDowngradeRule,
        StateDropRule,
    },
};
use anyhow::Result;
use parking_lot::Mutex;
use serde_json::json;

fn observe_hot(cm: &CostModel, site: &str, n: u32) {
    for _ in 0..n {
        cm.observe(CostModelUpdate {
            call_site_id: site.into(),
            input_tokens: 100,
            output_tokens: 40,
            latency_ms: 200.0,
            cost_usd: 0.01,
            output_is_structured: true,
            output_is_short: true,
            now_us: Some(0),
        });
    }
}

struct FixedKey;
impl CacheKeyBuilder for FixedKey {
    fn build(&self, call: &Call) -> CacheKey {
        CacheKey {
            prompt_hash: [0u8; 32],
            model: call.model.clone(),
            parameters_hash: [0u8; 32],
            call_site_id: call.call_site_id.clone(),
        }
    }
}

#[derive(Default)]
struct StubCache {
    next: Mutex<Option<CacheHit>>,
}
impl StubCache {
    fn set_hit(&self, value: CacheHit) {
        *self.next.lock() = Some(value);
    }
}
impl Cache for StubCache {
    fn lookup(&self, _key: &CacheKey, _now: i64) -> Result<Option<CacheHit>> {
        Ok(self.next.lock().clone())
    }
    fn insert(
        &self,
        _key: &CacheKey,
        _value: &CacheValue,
        _ttl: i64,
        _now: i64,
    ) -> Result<()> {
        Ok(())
    }
    fn invalidate(
        &self,
        _pattern: &agentc_memo::InvalidationPattern,
    ) -> Result<u64> {
        Ok(0)
    }
    fn stats(&self) -> Result<CacheStats> {
        Ok(CacheStats::default())
    }
}

fn state_drop_call() -> Call {
    // Four-message call: 2 of the messages are State-tagged "scratch"
    // that no downstream span reads. Fraction dropped = 0.5, projected
    // savings = 0.01 * 0.5 = 0.005.
    let messages = vec![
        Message { role: "system".into(), content: "sys".into() },
        Message { role: "user".into(), content: "keep".into() },
        Message { role: "user".into(), content: "stale-a".into() },
        Message { role: "user".into(), content: "stale-b".into() },
    ];
    let message_deps = json!([
        {"kind": "literal"},
        {"kind": "literal"},
        {"kind": "state", "key": "scratch"},
        {"kind": "state", "key": "scratch"},
    ]);
    let extra = json!({
        "message_deps": message_deps,
        "window_state_reads": [],
    });
    Call {
        call_site_id: "site".into(),
        trace_id: [0u8; 16],
        span_id: [0u8; 8],
        model: "gpt-4o".into(),
        messages,
        parameters: Parameters { extra, ..Default::default() },
        tools: vec![],
        input_deps: vec![DepSource::UserInput { span_id: [1u8; 8] }],
        occurrence_ix: 0,
    }
}

#[test]
fn cache_hit_beats_model_downgrade_when_both_fire() {
    let cm = Arc::new(CostModel::new());
    observe_hot(&cm, "site", 10);

    let cache: Arc<StubCache> = Arc::new(StubCache::default());
    cache.set_hit(CacheHit {
        value: CacheValue {
            output_content_id: "hit".into(),
            input_tokens: 0,
            output_tokens: 0,
            recorded_cost_usd: 0.0,
        },
        source: CacheSource::Exact,
        age_micros: 0,
    });

    let budget = Arc::new(Budget::new());
    // Seed 25 in-budget samples so ModelDowngrade exits probation and
    // would otherwise fire.
    for _ in 0..25 {
        budget.record_sample("site", "ModelDowngrade", 0.01, 1.0, 0);
    }

    let cache_hit: CacheHitRule =
        CacheHitRule::new(cache.clone(), Arc::new(FixedKey));
    let model_dg = ModelDowngradeRule::new(
        vec![ModelDowngradeRoute {
            from: "gpt-4o".into(),
            to: "gpt-4o-mini".into(),
            price_ratio: 0.5,
            max_output_tokens: 128,
        }],
        budget,
    );

    let opt = Optimizer::new(
        cm,
        vec![Box::new(cache_hit), Box::new(model_dg)],
        OptimizerConfig::default(),
    );

    let call = Call {
        call_site_id: "site".into(),
        trace_id: [0u8; 16],
        span_id: [0u8; 8],
        model: "gpt-4o".into(),
        messages: vec![],
        parameters: Parameters::default(),
        tools: vec![],
        input_deps: vec![],
        occurrence_ix: 0,
    };
    // cost.mean = 0.01 → CacheHit saves 0.01.
    // ModelDowngrade saves 0.01 * (1 - 0.5) = 0.005.
    match opt.plan(&call) {
        Plan::Cached { .. } => {}
        other => panic!("expected Cached (CacheHit wins), got {other:?}"),
    }
}

#[test]
fn state_drop_fires_when_only_applicable() {
    let cm = Arc::new(CostModel::new());
    observe_hot(&cm, "site", 10);

    let state_drop = StateDropRule::default();
    let opt = Optimizer::new(
        cm,
        vec![Box::new(state_drop)],
        OptimizerConfig::default(),
    );

    match opt.plan(&state_drop_call()) {
        Plan::Rewritten { rule, call, .. } => {
            assert_eq!(rule, "StateDrop");
            assert_eq!(call.messages.len(), 2);
        }
        other => panic!("expected Rewritten StateDrop, got {other:?}"),
    }
}

#[test]
fn higher_savings_rule_wins_when_both_safe() {
    // Force a scenario where StateDrop projects 0.005 and a fake
    // higher-savings rule projects more, so the planner picks it.
    // (This exercises planner ranking, not any single rule.)
    let cm = Arc::new(CostModel::new());
    observe_hot(&cm, "site", 10);

    // cost.mean = 0.01, dropped_fraction = 0.5 → StateDrop projects
    // 0.005. We wire a CacheHit stub that projects 0.01 (mean cost).
    let cache = Arc::new(StubCache::default());
    cache.set_hit(CacheHit {
        value: CacheValue {
            output_content_id: "hit".into(),
            input_tokens: 0,
            output_tokens: 0,
            recorded_cost_usd: 0.0,
        },
        source: CacheSource::Exact,
        age_micros: 0,
    });
    let cache_hit = CacheHitRule::new(cache, Arc::new(FixedKey));

    let opt = Optimizer::new(
        cm,
        vec![Box::new(StateDropRule::default()), Box::new(cache_hit)],
        OptimizerConfig::default(),
    );

    // Use the state_drop_call so StateDrop is also applicable.
    match opt.plan(&state_drop_call()) {
        Plan::Cached { .. } => {} // CacheHit wins on savings
        other => panic!("expected Cached, got {other:?}"),
    }
}

#[test]
fn planner_picks_highest_safe_saving_across_rule_set() {
    let cm = Arc::new(CostModel::new());
    // Bump the profile's cost mean so ModelDowngrade's projected
    // (cost * 0.9) beats CacheHit's projected cost (= mean). We do
    // that via a miss on the cache (CacheHit doesn't fire), forcing
    // ModelDowngrade to win by default.
    observe_hot(&cm, "site", 10);

    let miss_cache: Arc<StubCache> = Arc::new(StubCache::default());
    let cache_hit = CacheHitRule::new(miss_cache, Arc::new(FixedKey));

    let budget = Arc::new(Budget::new());
    for _ in 0..25 {
        budget.record_sample("site", "ModelDowngrade", 0.01, 1.0, 0);
    }
    let model_dg = ModelDowngradeRule::new(
        vec![ModelDowngradeRoute {
            from: "gpt-4o".into(),
            to: "gpt-4o-mini".into(),
            price_ratio: 0.1,
            max_output_tokens: 128,
        }],
        budget,
    );

    let opt = Optimizer::new(
        cm,
        vec![Box::new(cache_hit), Box::new(model_dg)],
        OptimizerConfig::default(),
    );

    let call = Call {
        call_site_id: "site".into(),
        trace_id: [0u8; 16],
        span_id: [0u8; 8],
        model: "gpt-4o".into(),
        messages: vec![],
        parameters: Parameters::default(),
        tools: vec![],
        input_deps: vec![],
        occurrence_ix: 0,
    };
    match opt.plan(&call) {
        Plan::Rewritten { rule, .. } => assert_eq!(rule, "ModelDowngrade"),
        other => panic!("expected Rewritten ModelDowngrade, got {other:?}"),
    }
}

/// Exit-criterion spot check: CacheHit on `Exact` source always
/// passes safety.
#[test]
fn cache_hit_exact_always_passes_safety() {
    use agentc_optimizer::planner::RewriteRule;

    let cache = Arc::new(StubCache::default());
    cache.set_hit(CacheHit {
        value: CacheValue {
            output_content_id: "x".into(),
            input_tokens: 1,
            output_tokens: 1,
            recorded_cost_usd: 0.0,
        },
        source: CacheSource::Exact,
        age_micros: 0,
    });
    let rule = CacheHitRule::new(cache, Arc::new(FixedKey));
    let mut profile = agentc_optimizer::cost_model::CallSiteProfile::new("site");
    profile.n_observations = 10;
    profile.cost_usd = WelfordStats::from_persisted(10, 0.01, 0.0);
    let proposal = rule
        .propose(
            &Call {
                call_site_id: "site".into(),
                trace_id: [0u8; 16],
                span_id: [0u8; 8],
                model: "gpt-4o".into(),
                messages: vec![],
                parameters: Parameters::default(),
                tools: vec![],
                input_deps: vec![],
                occurrence_ix: 0,
            },
            &profile,
        )
        .expect("must propose");
    assert!(matches!(proposal.rewritten, Plan::Cached { .. }));
    let dummy_call = Call {
        call_site_id: "site".into(),
        trace_id: [0u8; 16],
        span_id: [0u8; 8],
        model: "gpt-4o".into(),
        messages: vec![],
        parameters: Parameters::default(),
        tools: vec![],
        input_deps: vec![],
        occurrence_ix: 0,
    };
    assert!((proposal.safety_check)(&dummy_call));
}

/// Exit-criterion spot check: CacheHit rejects LSH hits below 0.95.
#[test]
fn cache_hit_lsh_below_threshold_rejected() {
    use agentc_optimizer::planner::RewriteRule;

    let cache = Arc::new(StubCache::default());
    cache.set_hit(CacheHit {
        value: CacheValue {
            output_content_id: "x".into(),
            input_tokens: 1,
            output_tokens: 1,
            recorded_cost_usd: 0.0,
        },
        source: CacheSource::Lsh { similarity: 0.92 },
        age_micros: 0,
    });
    let rule = CacheHitRule::new(cache, Arc::new(FixedKey));
    let mut profile = agentc_optimizer::cost_model::CallSiteProfile::new("site");
    profile.n_observations = 10;
    profile.cost_usd = WelfordStats::from_persisted(10, 0.01, 0.0);
    let dummy_call = Call {
        call_site_id: "site".into(),
        trace_id: [0u8; 16],
        span_id: [0u8; 8],
        model: "gpt-4o".into(),
        messages: vec![],
        parameters: Parameters::default(),
        tools: vec![],
        input_deps: vec![],
        occurrence_ix: 0,
    };
    assert!(rule.propose(&dummy_call, &profile).is_none());
}
