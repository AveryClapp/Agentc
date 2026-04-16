//! Shadow-mode sampling and divergence measurement.
//!
//! The optimizer runs a small fraction of optimized calls in parallel
//! with their unrewritten counterpart, discards the shadow result after
//! measuring divergence, and feeds the divergence into the accuracy
//! budget. We never block the user-visible call on the shadow.
//!
//! Spec § Architecture > Shadow mode. The sampling decision is per-call
//! Bernoulli; divergence is 1 - Jaccard on output tokens for text
//! outputs, and a structural comparison for tool calls.

use std::collections::HashSet;
use std::sync::atomic::{AtomicU64, Ordering};

use serde_json::Value;

/// Default sample rate from the spec (§ Configuration, `shadow_rate`).
pub const DEFAULT_SHADOW_RATE: f32 = 0.02;

/// Per-call Bernoulli decider. Exposed as a struct so tests can seed the
/// PRNG deterministically; production instances pick up a random seed
/// from the host clock.
///
/// We roll our own tiny xorshift64 because the optimizer already pays
/// careful attention to overhead on the hot path and we don't want to
/// depend on `rand` for a one-off Bernoulli.
pub struct ShadowSampler {
    rate: f32,
    state: AtomicU64,
}

impl ShadowSampler {
    pub fn new(rate: f32) -> Self {
        Self::with_seed(rate, default_seed())
    }

    pub fn with_seed(rate: f32, seed: u64) -> Self {
        // xorshift64 requires non-zero state.
        let seed = if seed == 0 { 0x9E3779B97F4A7C15 } else { seed };
        Self {
            rate: rate.clamp(0.0, 1.0),
            state: AtomicU64::new(seed),
        }
    }

    pub fn rate(&self) -> f32 {
        self.rate
    }

    /// Returns true when this call should run shadow. Uses an atomic
    /// load/xor/store sequence so concurrent callers may observe
    /// slightly correlated samples, but the ensemble still converges
    /// to the configured Bernoulli rate (verified in the sampling-rate
    /// test). We accept that correlation because the planner holds the
    /// sampler behind an `Arc` and hot-path mutex contention would
    /// dominate the divergence budget.
    pub fn should_sample(&self) -> bool {
        if self.rate <= 0.0 {
            return false;
        }
        if self.rate >= 1.0 {
            return true;
        }
        let mut s = self.state.load(Ordering::Relaxed);
        s ^= s << 13;
        s ^= s >> 7;
        s ^= s << 17;
        // Write back for next call. `Relaxed` ordering is fine — we
        // don't care about inter-thread visibility for a statistical
        // coin toss.
        self.state.store(s, Ordering::Relaxed);
        // Map to [0, 1). The top 53 bits of an xorshift64 give a
        // 2^{-53}-resolution uniform — plenty for a Bernoulli decision.
        let u = (s >> 11) as f64 / (1u64 << 53) as f64;
        (u as f32) < self.rate
    }
}

impl Default for ShadowSampler {
    fn default() -> Self {
        Self::new(DEFAULT_SHADOW_RATE)
    }
}

fn default_seed() -> u64 {
    use std::time::{SystemTime, UNIX_EPOCH};
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos() as u64)
        .unwrap_or(0x9E3779B97F4A7C15)
}

/// Text divergence = 1 - Jaccard on whitespace-delimited tokens.
/// Empty strings on both sides → 0 (identical). Empty on one side → 1.
///
/// This is the metric the spec promises (§ Architecture > Shadow mode);
/// its coarseness is intentional. We want "are these roughly the same
/// answer?" not "is this paraphrase equivalent?" — the latter requires
/// the eval harness (M9).
pub fn text_divergence(a: &str, b: &str) -> f32 {
    let set_a: HashSet<&str> = a.split_whitespace().collect();
    let set_b: HashSet<&str> = b.split_whitespace().collect();
    if set_a.is_empty() && set_b.is_empty() {
        return 0.0;
    }
    let inter = set_a.intersection(&set_b).count() as f32;
    let union = set_a.union(&set_b).count() as f32;
    if union == 0.0 {
        return 0.0;
    }
    1.0 - (inter / union)
}

/// Tool-call divergence: 1.0 if the called tool differs, else Jaccard on
/// the JSON-encoded argument keys + values. Spec-matches the rule that
/// a `ParallelBranch` reordering can't change tool names — if it does,
/// that's a 100% divergence.
pub fn tool_call_divergence(a: &ToolCall, b: &ToolCall) -> f32 {
    if a.name != b.name {
        return 1.0;
    }
    // Compare the serialized arguments as a token bag. Small, cheap,
    // and resistant to key-order variation.
    let at = serialize_args(&a.arguments);
    let bt = serialize_args(&b.arguments);
    text_divergence(&at, &bt)
}

/// Minimal tool-call representation. The SDK interceptor constructs
/// these from provider-native tool-call objects before handing them to
/// the divergence meter.
#[derive(Debug, Clone, PartialEq)]
pub struct ToolCall {
    pub name: String,
    pub arguments: Value,
}

fn serialize_args(v: &Value) -> String {
    // Flatten to "key:value" tokens so Jaccard treats argument shape
    // rather than raw JSON byte order.
    let mut tokens = Vec::new();
    flatten(v, "", &mut tokens);
    tokens.join(" ")
}

fn flatten(v: &Value, prefix: &str, out: &mut Vec<String>) {
    match v {
        Value::Object(map) => {
            for (k, sub) in map {
                let next = if prefix.is_empty() {
                    k.clone()
                } else {
                    format!("{prefix}.{k}")
                };
                flatten(sub, &next, out);
            }
        }
        Value::Array(items) => {
            for (i, sub) in items.iter().enumerate() {
                let next = format!("{prefix}[{i}]");
                flatten(sub, &next, out);
            }
        }
        Value::String(s) => out.push(format!("{prefix}:{s}")),
        Value::Number(n) => out.push(format!("{prefix}:{n}")),
        Value::Bool(b) => out.push(format!("{prefix}:{b}")),
        Value::Null => out.push(format!("{prefix}:null")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Exit-criterion: Bernoulli(0.02) fires at 2% ± 0.3% over 10k trials.
    #[test]
    fn sampling_rate_within_spec_tolerance() {
        let sampler = ShadowSampler::with_seed(0.02, 12345);
        let fires = (0..10_000).filter(|_| sampler.should_sample()).count();
        let rate = fires as f32 / 10_000.0;
        assert!(
            (rate - 0.02).abs() <= 0.003,
            "sample rate {rate} not within 2% ± 0.3%",
        );
    }

    #[test]
    fn zero_rate_never_samples() {
        let sampler = ShadowSampler::new(0.0);
        for _ in 0..1000 {
            assert!(!sampler.should_sample());
        }
    }

    #[test]
    fn one_rate_always_samples() {
        let sampler = ShadowSampler::new(1.0);
        for _ in 0..1000 {
            assert!(sampler.should_sample());
        }
    }

    #[test]
    fn identical_strings_have_zero_divergence() {
        assert_eq!(text_divergence("hello world", "hello world"), 0.0);
        assert_eq!(text_divergence("", ""), 0.0);
    }

    #[test]
    fn completely_different_strings_have_unit_divergence() {
        assert_eq!(text_divergence("foo bar", "baz qux"), 1.0);
    }

    #[test]
    fn partial_overlap_scales_linearly() {
        // "a b c" vs "a b d" — intersect = 2, union = 4, divergence = 0.5.
        assert!((text_divergence("a b c", "a b d") - 0.5).abs() < 1e-6);
    }

    #[test]
    fn different_tool_names_diverge_completely() {
        let a = ToolCall {
            name: "search".into(),
            arguments: serde_json::json!({"q": "x"}),
        };
        let b = ToolCall {
            name: "fetch".into(),
            arguments: serde_json::json!({"q": "x"}),
        };
        assert_eq!(tool_call_divergence(&a, &b), 1.0);
    }

    #[test]
    fn same_tool_same_args_have_zero_divergence() {
        let a = ToolCall {
            name: "search".into(),
            arguments: serde_json::json!({"q": "hello"}),
        };
        let b = ToolCall {
            name: "search".into(),
            arguments: serde_json::json!({"q": "hello"}),
        };
        assert_eq!(tool_call_divergence(&a, &b), 0.0);
    }

    #[test]
    fn same_tool_partial_arg_overlap_partial_divergence() {
        // {a:1, b:2} vs {a:1, b:3}: the "a.1" token overlaps, "b.2" and
        // "b.3" diverge. Two common tokens out of four → 1 - 2/4 - 2/4
        // wait: intersect={a:1}, union={a:1, b:2, b:3} → jaccard = 1/3,
        // divergence = 2/3.
        let a = ToolCall {
            name: "search".into(),
            arguments: serde_json::json!({"a": 1, "b": 2}),
        };
        let b = ToolCall {
            name: "search".into(),
            arguments: serde_json::json!({"a": 1, "b": 3}),
        };
        let d = tool_call_divergence(&a, &b);
        assert!(d > 0.0 && d < 1.0, "{d} should be partial");
    }

    #[test]
    fn same_tool_entirely_different_args_diverge_fully() {
        // "q:cats" and "q:dogs" share zero tokens → divergence 1.0.
        let a = ToolCall {
            name: "search".into(),
            arguments: serde_json::json!({"q": "cats"}),
        };
        let b = ToolCall {
            name: "search".into(),
            arguments: serde_json::json!({"q": "dogs"}),
        };
        assert_eq!(tool_call_divergence(&a, &b), 1.0);
    }
}
