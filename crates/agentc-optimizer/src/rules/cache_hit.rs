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
pub const SOURCE_POLICY_VERSION: &str = "memoized-response-source-v1";

/// Cache content, age and observed similarity are execution data. Only the
/// accepted source class and its fixed gate define the evidence cohort.
pub(crate) fn source_policy(value: &serde_json::Value) -> Option<serde_json::Value> {
    let source = value.get("source")?.as_object()?;
    match source.get("kind")?.as_str()? {
        "exact" if source.len() == 1 => Some(json!({"source_class": "exact"})),
        "lsh" if source.len() == 2 => {
            let similarity = source.get("similarity")?.as_f64()?;
            if !similarity.is_finite()
                || similarity < f64::from(LSH_SIMILARITY_THRESHOLD)
                || similarity > 1.0
            {
                return None;
            }
            Some(json!({"source_class": "lsh", "minimum_similarity": LSH_SIMILARITY_THRESHOLD}))
        }
        _ => None,
    }
}

/// A `CacheKeyBuilder` knows how to turn a `Call` into the
/// `CacheKey` that's compatible with the rest of the
/// caching pipeline. We take it as a trait-object so tests can inject a
/// deterministic key without pulling in the canonicalizer.
pub trait CacheKeyBuilder: Send + Sync {
    fn build(&self, call: &Call) -> Option<CacheKey>;
    /// Whether keys bind the original native message shape, rather than only
    /// its lossy DAG projection. Unsupported requests must still return None.
    fn supports_native_messages(&self) -> bool {
        false
    }
    fn identity(&self) -> Option<serde_json::Value> {
        None
    }
}

pub struct CacheHitRule {
    cache: Arc<dyn Cache>,
    key_builder: Arc<dyn CacheKeyBuilder>,
    now_micros: Arc<dyn Fn() -> i64 + Send + Sync>,
    accuracy_budget: f32,
    cache_policy_version: Option<String>,
}

impl CacheHitRule {
    pub fn new(cache: Arc<dyn Cache>, key_builder: Arc<dyn CacheKeyBuilder>) -> Self {
        Self {
            cache,
            key_builder,
            now_micros: Arc::new(default_now_micros),
            accuracy_budget: DEFAULT_ACCURACY_BUDGET,
            cache_policy_version: None,
        }
    }

    pub fn with_clock(mut self, clock: Arc<dyn Fn() -> i64 + Send + Sync>) -> Self {
        self.now_micros = clock;
        self.cache_policy_version = None;
        self
    }

    pub fn with_accuracy_budget(mut self, budget: f32) -> Self {
        self.accuracy_budget = budget;
        self
    }

    pub fn with_cache_policy_version(mut self, version: impl Into<String>) -> Self {
        self.cache_policy_version = Some(version.into());
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
    fn identity(&self) -> Option<crate::planner::RuleIdentity> {
        let policy = self
            .cache_policy_version
            .as_ref()
            .filter(|value| !value.is_empty())?;
        crate::planner::RuleIdentity::new(
            "memoized-response-lookup-v1",
            self.accuracy_budget,
            json!({
                "cache_policy_version": policy,
                "key_builder": self.key_builder.identity()?,
                "native_message_keys": self.key_builder.supports_native_messages(),
                "lsh_similarity_threshold": LSH_SIMILARITY_THRESHOLD,
            }),
        )
    }

    fn name(&self) -> &'static str {
        "CacheHit"
    }

    fn preserves_native_messages(&self) -> bool {
        self.key_builder.supports_native_messages()
    }

    fn applies(&self, _call: &Call, profile: &CallSiteProfile) -> bool {
        // A full retained window is the planner's precondition; here we only
        // skip if the site has never produced a cost observation — without
        // one, `cost_usd.mean` is 0 and the projected savings can't rank the
        // proposal.
        profile.n_observations > 0
    }

    fn propose(&self, call: &Call, profile: &CallSiteProfile) -> Option<Proposal> {
        let key = self.key_builder.build(call)?;
        let now = (self.now_micros)();
        let hit = self.cache.lookup(&key, now).ok().flatten()?;
        if !Self::passes_source_gate(&hit) {
            return None;
        }
        let projected = profile.cost_usd.mean as f32;
        let mut value = cache_hit_to_plan_value(&hit);
        if let Some(context) = call
            .parameters
            .extra
            .get(crate::model_catalog::ROUTE_CONTEXT_KEY)
        {
            if context.get("sdk_response_cache").is_some() {
                value["sdk_response_cache_version"] =
                    json!(crate::wiring::SDK_RESPONSE_CACHE_VERSION);
                value["provider_protocol"] = context["provider_protocol"].clone();
                value["provider_namespace"] = context["provider_namespace"].clone();
            }
        }
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
    use anyhow::Result;
    use parking_lot::Mutex;

    #[test]
    fn cache_identity_requires_declared_key_and_backend_policies() {
        struct DeclaredKey(&'static str);
        impl CacheKeyBuilder for DeclaredKey {
            fn build(&self, call: &Call) -> Option<CacheKey> {
                FixedKey.build(call)
            }
            fn identity(&self) -> Option<serde_json::Value> {
                Some(json!({"key_policy_version": self.0}))
            }
        }
        let cache = Arc::new(StubCache::default());
        assert!(CacheHitRule::new(cache.clone(), Arc::new(FixedKey))
            .with_cache_policy_version("backend-v1")
            .identity()
            .is_none());
        assert!(
            CacheHitRule::new(cache.clone(), Arc::new(DeclaredKey("key-v1")))
                .identity()
                .is_none()
        );
        let configured = |key, backend| {
            CacheHitRule::new(cache.clone(), Arc::new(DeclaredKey(key)))
                .with_cache_policy_version(backend)
        };
        let baseline = configured("key-v1", "backend-v1").identity().unwrap();
        assert_ne!(
            baseline,
            configured("key-v2", "backend-v1").identity().unwrap()
        );
        assert_ne!(
            baseline,
            configured("key-v1", "backend-v2").identity().unwrap()
        );
        assert_ne!(
            baseline,
            configured("key-v1", "backend-v1")
                .with_accuracy_budget(0.02)
                .identity()
                .unwrap()
        );
        assert!(configured("key-v1", "backend-v1")
            .with_clock(Arc::new(|| 100))
            .identity()
            .is_none());
    }

    #[test]
    fn cache_native_message_support_is_explicit_and_identity_bound() {
        struct NativeKey(bool);
        impl CacheKeyBuilder for NativeKey {
            fn build(&self, call: &Call) -> Option<CacheKey> {
                FixedKey.build(call)
            }
            fn identity(&self) -> Option<serde_json::Value> {
                Some(json!({"key_version":"fixture-v1"}))
            }
            fn supports_native_messages(&self) -> bool {
                self.0
            }
        }
        let make = |native| {
            CacheHitRule::new(Arc::new(StubCache::default()), Arc::new(NativeKey(native)))
                .with_cache_policy_version("backend-v1")
        };
        assert!(!make(false).preserves_native_messages());
        assert!(make(true).preserves_native_messages());
        assert_ne!(make(false).identity(), make(true).identity());
        assert!(
            !CacheHitRule::new(Arc::new(StubCache::default()), Arc::new(FixedKey))
                .preserves_native_messages()
        );
    }

    fn zero_hash() -> [u8; 32] {
        [0u8; 32]
    }

    struct FixedKey;
    impl CacheKeyBuilder for FixedKey {
        fn build(&self, call: &Call) -> Option<CacheKey> {
            Some(CacheKey {
                prompt_hash: zero_hash(),
                model: call.model.clone(),
                parameters_hash: zero_hash(),
                call_site_id: call.call_site_id.clone(),
            })
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
        fn lookup(&self, _key: &CacheKey, _now: i64) -> Result<Option<CacheHit>> {
            Ok(self.next.lock().clone())
        }
        fn insert(&self, _key: &CacheKey, _value: &CacheValue, _ttl: i64, _now: i64) -> Result<()> {
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
