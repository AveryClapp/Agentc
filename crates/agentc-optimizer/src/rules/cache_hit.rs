//! `CacheHit` — serve the canonical form of a call out of the memoization
//! cache instead of dispatching to the model.
//!
//! Safety model (spec § Rule specifications > CacheHit):
//! - `Exact` hits always pass (the memoization cache already enforces TTL).
//! - `Lsh` hits require `similarity >= 0.95`, tighter than memoize's own
//!   default threshold — the optimizer's budget is stricter than opt-in
//!   memoize.
//!
//! The cache lookup is the applicability test AND the source of the
//! rewrite payload, so we cache the `CacheHit` on the proposal via the
//! safety-check closure: by the time the planner asks for safety, we've
//! already done the DB read.

use std::sync::Arc;

use agentc_memo::{Cache, CacheHit, CacheKey, CacheSource};
use serde_json::json;

use crate::cost_model::CallSiteProfile;
use crate::dag::Call;
use crate::planner::{CostDriver, Plan, Proposal, RewriteRule};

/// LSH similarity threshold the optimizer requires — spec-mandated 0.95.
pub const LSH_SIMILARITY_THRESHOLD: f32 = 0.95;

/// Default accuracy budget from spec § Accuracy budget.
pub const DEFAULT_ACCURACY_BUDGET: f32 = 0.01;

/// A `CacheKeyBuilder` knows how to turn a `Call` into the
/// `CacheKey` that's compatible with the rest of the
/// caching pipeline. We take it as a trait-object so tests can inject a
/// deterministic key without pulling in the canonicalizer.
pub trait CacheKeyBuilder: Send + Sync {
    fn build(&self, call: &Call) -> CacheKey;
}

pub struct CacheHitRule {
    cache: Arc<dyn Cache>,
    key_builder: Arc<dyn CacheKeyBuilder>,
    now_micros: Arc<dyn Fn() -> i64 + Send + Sync>,
    accuracy_budget: f32,
}

impl CacheHitRule {
    pub fn new(cache: Arc<dyn Cache>, key_builder: Arc<dyn CacheKeyBuilder>) -> Self {
        Self {
            cache,
            key_builder,
            now_micros: Arc::new(default_now_micros),
            accuracy_budget: DEFAULT_ACCURACY_BUDGET,
        }
    }

    pub fn with_clock(mut self, clock: Arc<dyn Fn() -> i64 + Send + Sync>) -> Self {
        self.now_micros = clock;
        self
    }

    pub fn with_accuracy_budget(mut self, budget: f32) -> Self {
        self.accuracy_budget = budget;
        self
    }

    fn passes_source_gate(hit: &CacheHit) -> bool {
        match hit.source {
            CacheSource::Exact => true,
            CacheSource::Lsh { similarity } => similarity >= LSH_SIMILARITY_THRESHOLD,
        }
    }
}

impl RewriteRule for CacheHitRule {
    fn name(&self) -> &'static str {
        "CacheHit"
    }

    fn applies(&self, _call: &Call, profile: &CallSiteProfile) -> bool {
        // The profile's n_observations >= hot_threshold is the planner's
        // precondition; here we only skip if the site has never produced a
        // cost observation — without one, `cost_usd.mean` is 0 and the
        // projected savings can't rank the proposal.
        profile.n_observations > 0
    }

    fn propose(&self, call: &Call, profile: &CallSiteProfile) -> Option<Proposal> {
        let key = self.key_builder.build(call);
        let now = (self.now_micros)();
        let hit = self.cache.lookup(&key, now).ok().flatten()?;
        if !Self::passes_source_gate(&hit) {
            return None;
        }
        let projected = profile.cost_usd.mean as f32;
        let value = cache_hit_to_plan_value(&hit);
        Some(Proposal {
            rewritten: Plan::Cached { value },
            projected_savings_usd: projected,
            cost_driver: CostDriver::CallElimination,
            // Source gate already ran; the memoization cache enforces TTL
            // on lookup, so safety is structurally guaranteed.
            safety_check: Box::new(|_| true),
        })
    }

    fn accuracy_budget(&self) -> f32 {
        self.accuracy_budget
    }
}

fn cache_hit_to_plan_value(hit: &CacheHit) -> serde_json::Value {
    json!({
        "output_content_id": hit.value.output_content_id,
        "input_tokens": hit.value.input_tokens,
        "output_tokens": hit.value.output_tokens,
        "recorded_cost_usd": hit.value.recorded_cost_usd,
        "source": describe_source(hit.source),
        "age_micros": hit.age_micros,
    })
}

fn describe_source(src: CacheSource) -> serde_json::Value {
    match src {
        CacheSource::Exact => json!({"kind": "exact"}),
        CacheSource::Lsh { similarity } => json!({"kind": "lsh", "similarity": similarity}),
    }
}

fn default_now_micros() -> i64 {
    use std::time::{SystemTime, UNIX_EPOCH};
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_micros() as i64)
        .unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;
    use agentc_memo::{CacheStats, CacheValue, InvalidationPattern};
    use parking_lot::Mutex;
    use anyhow::Result;

    fn zero_hash() -> [u8; 32] {
        [0u8; 32]
    }

    struct FixedKey;
    impl CacheKeyBuilder for FixedKey {
        fn build(&self, call: &Call) -> CacheKey {
            CacheKey {
                prompt_hash: zero_hash(),
                model: call.model.clone(),
                parameters_hash: zero_hash(),
                call_site_id: call.call_site_id.clone(),
            }
        }
    }

    #[derive(Default)]
    struct StubCache {
        next: Mutex<Option<CacheHit>>,
    }
    impl StubCache {
        fn set(&self, hit: Option<CacheHit>) {
            *self.next.lock() = hit;
        }
    }
    impl Cache for StubCache {
        fn lookup(
            &self,
            _key: &CacheKey,
            _now: i64,
        ) -> Result<Option<CacheHit>> {
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
        fn invalidate(&self, _pattern: &InvalidationPattern) -> Result<u64> {
            Ok(0)
        }
        fn stats(&self) -> Result<CacheStats> {
            Ok(CacheStats::default())
        }
    }

    fn sample_call() -> Call {
        Call {
            call_site_id: "site".into(),
            trace_id: [0u8; 16],
            span_id: [0u8; 8],
            model: "gpt-4o".into(),
            messages: vec![],
            parameters: Default::default(),
            tools: vec![],
            input_deps: vec![],
            occurrence_ix: 0,
        }
    }

    fn hot_profile(cost: f64) -> CallSiteProfile {
        let mut p = CallSiteProfile::new("site");
        // Force a non-zero observation count + cost.mean without running
        // a full Welford update — mean is what the rule reads.
        p.n_observations = 10;
        p.cost_usd = crate::cost_model::WelfordStats::from_persisted(10, cost, 0.0);
        p
    }

    fn stub_hit(source: CacheSource) -> CacheHit {
        CacheHit {
            value: CacheValue {
                output_content_id: "content-abc".into(),
                input_tokens: 10,
                output_tokens: 5,
                recorded_cost_usd: 0.001,
            },
            source,
            age_micros: 1_000,
        }
    }

    #[test]
    fn exact_hit_always_passes() {
        let stub = Arc::new(StubCache::default());
        stub.set(Some(stub_hit(CacheSource::Exact)));
        let rule = CacheHitRule::new(stub, Arc::new(FixedKey));
        let proposal = rule
            .propose(&sample_call(), &hot_profile(0.002))
            .expect("must propose");
        assert!(matches!(proposal.rewritten, Plan::Cached { .. }));
        assert!((proposal.safety_check)(&sample_call()));
    }

    #[test]
    fn lsh_hit_below_threshold_is_rejected() {
        let stub = Arc::new(StubCache::default());
        stub.set(Some(stub_hit(CacheSource::Lsh { similarity: 0.90 })));
        let rule = CacheHitRule::new(stub, Arc::new(FixedKey));
        assert!(rule.propose(&sample_call(), &hot_profile(0.002)).is_none());
    }

    #[test]
    fn lsh_hit_at_or_above_threshold_passes() {
        let stub = Arc::new(StubCache::default());
        stub.set(Some(stub_hit(CacheSource::Lsh { similarity: 0.95 })));
        let rule = CacheHitRule::new(stub, Arc::new(FixedKey));
        let proposal = rule
            .propose(&sample_call(), &hot_profile(0.002))
            .expect("0.95 meets threshold");
        assert!(matches!(proposal.rewritten, Plan::Cached { .. }));
    }

    #[test]
    fn miss_produces_no_proposal() {
        let stub = Arc::new(StubCache::default());
        stub.set(None);
        let rule = CacheHitRule::new(stub, Arc::new(FixedKey));
        assert!(rule.propose(&sample_call(), &hot_profile(0.002)).is_none());
    }

    #[test]
    fn projected_savings_is_mean_cost() {
        let stub = Arc::new(StubCache::default());
        stub.set(Some(stub_hit(CacheSource::Exact)));
        let rule = CacheHitRule::new(stub, Arc::new(FixedKey));
        let p = rule
            .propose(&sample_call(), &hot_profile(0.0042))
            .expect("must propose");
        assert!((p.projected_savings_usd - 0.0042).abs() < 1e-6);
    }
}
