from __future__ import annotations

import math
import random

import pytest

from agentc.change_safety.detectors import (
    ConfidenceSequenceDetector,
    ConsecutiveBreachGuard,
    CusumDetector,
    ExposureGuard,
    SprtDetector,
)


def _run(detector, stream) -> bool:  # type: ignore[no-untyped-def]
    detector.reset()
    for x in stream:
        if detector.update(x):
            return True
    return False


def _slack(p: float, n: int) -> float:
    return 3.0 * math.sqrt(p * (1.0 - p) / n)


# --- confidence sequence ----------------------------------------------------


@pytest.mark.parametrize("betting", ["agrapa", "prpl"])
@pytest.mark.parametrize("kind", ["bernoulli", "continuous"])
def test_cs_false_alarm_under_null_stays_below_alpha(betting: str, kind: str) -> None:
    alpha = 0.1
    m = 0.3  # true mean sits exactly on the null boundary mu0 + margin
    detector = ConfidenceSequenceDetector(0.25, 0.05, alpha=alpha, betting=betting)
    rng = random.Random(1)
    n_streams, length = 400, 1500
    alarms = 0
    for _ in range(n_streams):
        if kind == "bernoulli":
            stream = (1.0 if rng.random() < m else 0.0 for _ in range(length))
        else:
            stream = (rng.betavariate(3.0, 7.0) for _ in range(length))  # mean 0.3
        alarms += _run(detector, stream)
    assert alarms / n_streams <= alpha + _slack(alpha, n_streams)


def test_cs_guarantee_holds_at_every_stopping_time() -> None:
    # Ville: P(sup_t K_t >= 1/alpha) <= alpha, so the alarm rate counted over
    # a very long horizon (the worst stopping time) is still bounded.
    alpha = 0.05
    detector = ConfidenceSequenceDetector(0.0, 0.2, alpha=alpha)
    rng = random.Random(7)
    n_streams = 300
    alarms = sum(_run(detector, (1.0 if rng.random() < 0.2 else 0.0 for _ in range(4000))) for _ in range(n_streams))
    assert alarms / n_streams <= alpha + _slack(alpha, n_streams)


def test_cs_detects_shift_and_capital_stays_finite() -> None:
    detector = ConfidenceSequenceDetector.calibrate([0.0] * 90 + [1.0] * 10, margin=0.05, alpha=0.05)
    assert detector.null_mean == pytest.approx(0.15)
    rng = random.Random(3)
    delays = []
    for _ in range(100):
        detector.reset()
        while not detector.update(1.0 if rng.random() < 0.6 else 0.0):
            pass
        delays.append(detector.alarm_at)
    assert max(delays) < 60  # type: ignore[type-var]
    assert 0.0 <= detector.bet() <= detector.max_bet


def test_cs_never_alarms_below_null() -> None:
    detector = ConfidenceSequenceDetector(0.3, 0.0, alpha=0.05)
    assert not _run(detector, [0.0] * 5000)


# --- CUSUM -------------------------------------------------------------------


def test_cusum_calibration_hits_target_false_alarm_and_detects_shift() -> None:
    rng = random.Random(11)
    aa = [rng.betavariate(2, 18) for _ in range(500)]  # mean 0.1
    target, horizon = 0.05, 100
    detector = CusumDetector.calibrate(aa, horizon=horizon, target_false_alarm=target, n_sim=4000, seed=5)
    assert detector.mu0 == pytest.approx(sum(aa) / len(aa))
    assert detector.h > 0
    n = 1000
    false = sum(_run(detector, rng.choices(aa, k=horizon)) for _ in range(n))
    assert false / n <= target + _slack(target, n)
    caught = sum(_run(detector, (rng.betavariate(5, 10) for _ in range(horizon))) for _ in range(200))
    assert caught == 200


def test_cusum_recursion_by_hand() -> None:
    detector = CusumDetector(mu0=0.1, k=0.05, h=0.5)
    detector.update(0.4)
    assert detector.statistic == pytest.approx(0.25)
    detector.update(0.0)
    assert detector.statistic == pytest.approx(0.1)
    detector.update(0.0)
    assert detector.statistic == 0.0
    assert not detector.update(0.6)
    assert detector.update(0.6)  # 0.45 + 0.45 = 0.9 > 0.5
    assert detector.alarm_at == 5


# --- SPRT --------------------------------------------------------------------


def test_sprt_calibration_and_error_rates() -> None:
    aa = [0.0] * 95 + [1.0] * 5
    detector = SprtDetector.calibrate(aa, delta=0.15, alpha=0.05, beta=0.1)
    assert detector.p0 == pytest.approx(5.5 / 101)
    assert detector.p1 == pytest.approx(5.5 / 101 + 0.15)
    rng = random.Random(2)
    n = 2000
    false = sum(_run(detector, (1.0 if rng.random() < detector.p0 else 0.0 for _ in range(3000))) for _ in range(n))
    assert false / n <= 0.05 / 0.9 + _slack(0.05, n)
    hits = sum(_run(detector, (1.0 if rng.random() < detector.p1 else 0.0 for _ in range(3000))) for _ in range(n))
    assert hits / n >= 0.9 - _slack(0.1, n)
    loud = sum(_run(detector, [1.0] * 50) for _ in range(5))
    assert loud == 5


def test_sprt_settles_on_h0() -> None:
    detector = SprtDetector(0.05, 0.3)
    for _ in range(200):
        detector.update(0.0)
    assert detector.settled and not detector.alarmed
    assert not any(detector.update(1.0) for _ in range(50))


def test_sprt_all_zero_aa_gives_finite_llr() -> None:
    detector = SprtDetector.calibrate([0.0] * 30, delta=0.1)
    assert 0 < detector.p0 < detector.p1 < 1
    assert math.isfinite(detector.llr_one)


# --- existing guard emulations ----------------------------------------------


def test_exposure_guard_matches_rust_contract() -> None:
    guard = ExposureGuard(0.05)
    assert not guard.update(0.55, t=0.0)
    assert guard.exposure == pytest.approx(0.5)
    assert not guard.update(0.01, t=1.0)  # below threshold: not retained
    assert len(guard.samples) == 1
    assert guard.update(0.55, t=2.0)  # 1.0 >= 1.0
    # Rolling window: a sample exactly one window old is pruned (retain observed_at > cutoff).
    guard = ExposureGuard(0.05, window=86400.0)
    guard.update(0.55, t=0.0)
    assert not guard.update(0.55, t=86400.0)
    guard = ExposureGuard(0.05, window=86400.0)
    guard.update(0.55, t=0.0)
    assert guard.update(0.55, t=86399.5)


def test_consecutive_breach_resets_and_uses_f32() -> None:
    guard = ConsecutiveBreachGuard(0.03)
    for _ in range(4):
        assert not guard.update(0.5)
    assert not guard.update(0.0)  # within budget resets the streak
    assert guard.consecutive == 0
    for i in range(5):
        alarmed = guard.update(0.5)
    assert alarmed and guard.alarm_at == 10
    # Equal after f32 rounding is not a breach, as in budget.rs.
    guard = ConsecutiveBreachGuard(0.03)
    assert 0.03 + 1e-12 > 0.03
    for _ in range(10):
        guard.update(0.03 + 1e-12)
    assert guard.consecutive == 0 and not guard.alarmed


def test_interface_latches_resets_and_validates() -> None:
    guard = ConsecutiveBreachGuard(0.0, streak=1)
    assert guard.update(1.0)
    assert guard.update(0.0) and guard.n == 1  # latched; later values ignored
    guard.reset()
    assert not guard.alarmed and guard.n == 0 and guard.alarm_at is None
    for bad in (-0.1, 1.5, math.nan):
        with pytest.raises(ValueError):
            guard.update(bad)
