//! Shadow-mode sampling.
//!
//! The optimizer runs a small fraction of optimized calls in parallel with
//! their unrewritten counterpart, measures how far the two outputs diverged,
//! and feeds that into the accuracy budget. We never block the user-visible
//! call on the shadow. This module owns the per-call Bernoulli sampling
//! decision ([`ShadowSampler`]); the divergence metric itself is computed on
//! the Python interceptor side and recorded via the `optimize_record_divergence`
//! FFI.
//!
//! Spec § Architecture > Shadow mode.

use std::sync::atomic::{AtomicU64, Ordering};

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
}
