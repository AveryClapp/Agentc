//! Nonblocking admission control for synchronous optimizer work.
//!
//! The planner is deliberately CPU-bounded, but releasing the Python GIL lets
//! a burst create more runnable Rust work than the host can execute. Once all
//! permits are occupied, new calls fail open immediately instead of joining a
//! scheduler queue whose delay the internal planning deadline cannot observe.

use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};

/// Point-in-time process-local admission telemetry.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) struct PlannerAdmissionStats {
    pub(crate) attempted: u64,
    pub(crate) admitted: u64,
    pub(crate) rejected_saturated: u64,
    pub(crate) inflight: usize,
    pub(crate) max_observed_inflight: usize,
    pub(crate) limit: usize,
}

/// Owns the permit count and its observability counters.
pub(crate) struct PlannerAdmission {
    limit: usize,
    inflight: AtomicUsize,
    max_observed_inflight: AtomicUsize,
    attempted: AtomicU64,
    admitted: AtomicU64,
    rejected_saturated: AtomicU64,
}

impl PlannerAdmission {
    pub(crate) fn new(limit: usize) -> Self {
        assert!(limit > 0, "planner admission limit must be positive");
        Self {
            limit,
            inflight: AtomicUsize::new(0),
            max_observed_inflight: AtomicUsize::new(0),
            attempted: AtomicU64::new(0),
            admitted: AtomicU64::new(0),
            rejected_saturated: AtomicU64::new(0),
        }
    }

    /// Acquire one permit without waiting. The returned guard releases it.
    pub(crate) fn try_acquire(&self) -> Option<PlannerPermit<'_>> {
        self.attempted.fetch_add(1, Ordering::Relaxed);
        let mut current = self.inflight.load(Ordering::Relaxed);
        loop {
            if current >= self.limit {
                self.rejected_saturated.fetch_add(1, Ordering::Relaxed);
                return None;
            }
            match self.inflight.compare_exchange_weak(
                current,
                current + 1,
                Ordering::Acquire,
                Ordering::Relaxed,
            ) {
                Ok(_) => {
                    self.admitted.fetch_add(1, Ordering::Relaxed);
                    self.max_observed_inflight
                        .fetch_max(current + 1, Ordering::Relaxed);
                    return Some(PlannerPermit { admission: self });
                }
                Err(observed) => current = observed,
            }
        }
    }

    pub(crate) fn stats(&self) -> PlannerAdmissionStats {
        PlannerAdmissionStats {
            attempted: self.attempted.load(Ordering::Relaxed),
            admitted: self.admitted.load(Ordering::Relaxed),
            rejected_saturated: self.rejected_saturated.load(Ordering::Relaxed),
            inflight: self.inflight.load(Ordering::Relaxed),
            max_observed_inflight: self.max_observed_inflight.load(Ordering::Relaxed),
            limit: self.limit,
        }
    }

    pub(crate) fn limit(&self) -> usize {
        self.limit
    }
}

pub(crate) struct PlannerPermit<'a> {
    admission: &'a PlannerAdmission,
}

impl Drop for PlannerPermit<'_> {
    fn drop(&mut self) {
        self.admission.inflight.fetch_sub(1, Ordering::Release);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn saturated_admission_fails_without_waiting_and_recovers() {
        let admission = PlannerAdmission::new(1);
        let permit = admission.try_acquire().expect("first call is admitted");
        assert!(admission.try_acquire().is_none());
        assert_eq!(
            admission.stats(),
            PlannerAdmissionStats {
                attempted: 2,
                admitted: 1,
                rejected_saturated: 1,
                inflight: 1,
                max_observed_inflight: 1,
                limit: 1,
            }
        );

        drop(permit);
        assert!(admission.try_acquire().is_some());
        let stats = admission.stats();
        assert_eq!(stats.attempted, 3);
        assert_eq!(stats.admitted, 2);
        assert_eq!(stats.rejected_saturated, 1);
    }

    #[test]
    fn max_observed_inflight_never_exceeds_limit() {
        let admission = PlannerAdmission::new(2);
        let first = admission.try_acquire().unwrap();
        let second = admission.try_acquire().unwrap();
        assert!(admission.try_acquire().is_none());
        assert_eq!(admission.stats().max_observed_inflight, 2);
        drop((first, second));
        assert_eq!(admission.stats().inflight, 0);
    }
}
