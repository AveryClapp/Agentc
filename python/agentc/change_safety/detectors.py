"""Online change detectors over per-call fork signals (protocol Section 6.2).

Each detector consumes one signal value per sampled fork, in arrival order,
and answers one question: should the change be disabled now? All signals are
divergences in ``[0, 1]`` (see :mod:`agentc.change_safety.records`), so every
detector here alarms on *large* values.

Common interface (:class:`Detector`):

- ``update(x, t=None) -> bool``: fold one value; returns whether the detector
  is alarmed. Alarms latch: once alarmed, later values are ignored until
  ``reset()``.
- ``alarmed``, ``n`` (values folded so far), ``alarm_at`` (the ``n`` at which
  the alarm fired, or ``None``), ``reset()``.

Detectors:

- :class:`CusumDetector`: one-sided CUSUM on excess signal, threshold
  calibrated by bootstrap simulation on A/A values.
- :class:`SprtDetector`: Wald SPRT on a binary disagreement indicator.
- :class:`ConfidenceSequenceDetector`: anytime-valid one-sided betting
  confidence sequence for a bounded mean (Waudby-Smith and Ramdas 2023).
- :class:`ExposureGuard`: emulation of Agentc's complete-plan exposure guard.
- :class:`ConsecutiveBreachGuard`: emulation of Agentc's five-consecutive-
  breach compatibility controller.

This module is pure standard library; it ships in the SDK.
"""

from __future__ import annotations

import math
import random
import struct
from abc import ABC, abstractmethod
from collections import deque
from typing import Sequence


def _unit(x: float) -> float:
    value = float(x)
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError(f"signal value must be a finite number in [0, 1], got {x!r}")
    return value


def _mean(values: Sequence[float]) -> float:
    return math.fsum(values) / len(values)


def _sd(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    mu = _mean(values)
    return math.sqrt(math.fsum((v - mu) ** 2 for v in values) / (len(values) - 1))


def _aa_values(values: Sequence[float]) -> list[float]:
    cleaned = [_unit(v) for v in values]
    if not cleaned:
        raise ValueError("calibration needs at least one A/A value")
    return cleaned


class Detector(ABC):
    """Latching online detector over a stream of ``[0, 1]`` signal values."""

    def __init__(self) -> None:
        self._n = 0
        self._alarm_at: int | None = None

    @property
    def n(self) -> int:
        """Number of values folded before (and including) the alarm."""
        return self._n

    @property
    def alarmed(self) -> bool:
        return self._alarm_at is not None

    @property
    def alarm_at(self) -> int | None:
        """1-based count of folded values at which the alarm fired."""
        return self._alarm_at

    def update(self, x: float, *, t: float | None = None) -> bool:
        """Fold one signal value observed at time ``t``; return ``alarmed``.

        ``t`` is used only by :class:`ExposureGuard`; when omitted the value's
        1-based index serves as its clock.
        """
        value = _unit(x)
        if self._alarm_at is not None:
            return True
        self._n += 1
        if self._step(value, float(self._n) if t is None else float(t)):
            self._alarm_at = self._n
        return self._alarm_at is not None

    def reset(self) -> None:
        self._n = 0
        self._alarm_at = None
        self._reset_state()

    @abstractmethod
    def _step(self, x: float, t: float) -> bool:
        """Fold one validated value; return True to alarm."""

    @abstractmethod
    def _reset_state(self) -> None:
        """Clear detector-specific state."""


# ---------------------------------------------------------------------------
# CUSUM
# ---------------------------------------------------------------------------


class CusumDetector(Detector):
    """One-sided (upper) CUSUM on excess signal.

    ``S_0 = 0``, ``S_t = max(0, S_{t-1} + x_t - mu0 - k)``; alarm when
    ``S_t > h`` (Page 1954). ``mu0`` is the A/A mean, ``k`` the reference
    allowance (half the smallest mean shift worth detecting).

    Calibration rule (:meth:`calibrate`): resample ``n_sim`` streams of length
    ``horizon`` i.i.d. with replacement from the A/A values, record each
    stream's ``max_t S_t``, and set ``h`` to the order statistic
    ``M_(ceil((1 - target) * n_sim))``. Because the alarm is strict
    (``S_t > h``), at most ``floor(target * n_sim)`` simulated A/A streams
    alarm, so the bootstrap estimate of
    ``P_AA(alarm within horizon sampled calls)`` is at most ``target``. This
    treats A/A values as exchangeable; serial dependence within a trajectory
    is not modelled.
    """

    def __init__(self, mu0: float, k: float, h: float) -> None:
        super().__init__()
        if not all(math.isfinite(v) for v in (mu0, k, h)) or k < 0.0 or h < 0.0:
            raise ValueError("CUSUM needs finite mu0, k >= 0 and h >= 0")
        self.mu0 = float(mu0)
        self.k = float(k)
        self.h = float(h)
        self.statistic = 0.0

    @classmethod
    def calibrate(
        cls,
        aa_values: Sequence[float],
        *,
        horizon: int,
        target_false_alarm: float = 0.05,
        shift: float | None = None,
        n_sim: int = 2000,
        seed: int = 0,
    ) -> CusumDetector:
        """Calibrate ``(mu0, k, h)`` from A/A values.

        ``mu0`` is the A/A mean. ``k = shift / 2`` when ``shift`` is given,
        otherwise half the A/A standard deviation (tuned to a one-SD shift).
        ``h`` follows the bootstrap rule in the class docstring.
        """
        values = _aa_values(aa_values)
        if horizon < 1 or n_sim < 1 or not 0.0 < target_false_alarm < 1.0:
            raise ValueError("need horizon >= 1, n_sim >= 1, 0 < target < 1")
        mu0 = _mean(values)
        k = 0.5 * (shift if shift is not None else _sd(values))
        offset = mu0 + k
        rng = random.Random(seed)
        maxima: list[float] = []
        for _ in range(n_sim):
            s = 0.0
            peak = 0.0
            for x in rng.choices(values, k=horizon):
                s = max(0.0, s + x - offset)
                if s > peak:
                    peak = s
            maxima.append(peak)
        maxima.sort()
        index = max(0, math.ceil((1.0 - target_false_alarm) * n_sim) - 1)
        return cls(mu0=mu0, k=k, h=maxima[index])

    def _step(self, x: float, t: float) -> bool:
        self.statistic = max(0.0, self.statistic + x - self.mu0 - self.k)
        return self.statistic > self.h

    def _reset_state(self) -> None:
        self.statistic = 0.0


# ---------------------------------------------------------------------------
# SPRT
# ---------------------------------------------------------------------------


class SprtDetector(Detector):
    """Wald sequential probability ratio test on a disagreement indicator.

    Each value becomes ``y = 1 if x > indicator_threshold else 0`` (for S4,
    ``x > 0`` means the fork's action disagreed). The test is
    ``H0: p = p0`` against ``H1: p = p1`` with log-likelihood ratio
    ``L_t = sum y log(p1/p0) + (1 - y) log((1-p1)/(1-p0))`` and Wald's
    boundaries ``A = log((1 - beta)/alpha)``, ``B = log(beta/(1 - alpha))``
    (Wald 1945). ``L_t >= A`` alarms. ``L_t <= B`` accepts H0: the detector
    then settles and stays quiet until ``reset()``; it never restarts, so
    Wald's error bound ``P_H0(alarm) <= alpha / (1 - beta)`` applies to the
    whole stream.
    """

    def __init__(
        self,
        p0: float,
        p1: float,
        *,
        alpha: float = 0.05,
        beta: float = 0.10,
        indicator_threshold: float = 0.0,
    ) -> None:
        super().__init__()
        if not 0.0 < p0 < p1 < 1.0:
            raise ValueError("SPRT needs 0 < p0 < p1 < 1")
        if not (0.0 < alpha < 1.0 and 0.0 < beta < 1.0 and alpha + beta < 1.0):
            raise ValueError("SPRT needs 0 < alpha, beta and alpha + beta < 1")
        self.p0 = float(p0)
        self.p1 = float(p1)
        self.alpha = float(alpha)
        self.beta = float(beta)
        self.indicator_threshold = float(indicator_threshold)
        self.upper = math.log((1.0 - beta) / alpha)
        self.lower = math.log(beta / (1.0 - alpha))
        self.llr_one = math.log(p1 / p0)
        self.llr_zero = math.log((1.0 - p1) / (1.0 - p0))
        self.llr = 0.0
        self.settled = False

    @classmethod
    def calibrate(
        cls,
        aa_values: Sequence[float],
        *,
        delta: float,
        alpha: float = 0.05,
        beta: float = 0.10,
        indicator_threshold: float = 0.0,
    ) -> SprtDetector:
        """Set ``p0`` from the A/A indicator rate and ``p1 = p0 + delta``.

        ``p0 = (d + 1/2) / (n + 1)`` for ``d`` disagreements among ``n`` A/A
        values (the Jeffreys posterior mean), so an all-agree A/A sample does
        not give ``p0 = 0`` and an infinite log-likelihood ratio. ``p1`` is
        capped at ``1 - 1e-6``.
        """
        values = _aa_values(aa_values)
        if delta <= 0.0:
            raise ValueError("delta must be positive")
        hits = sum(1 for v in values if v > indicator_threshold)
        p0 = (hits + 0.5) / (len(values) + 1.0)
        p1 = min(p0 + delta, 1.0 - 1e-6)
        return cls(p0, p1, alpha=alpha, beta=beta, indicator_threshold=indicator_threshold)

    def _step(self, x: float, t: float) -> bool:
        if self.settled:
            return False
        self.llr += self.llr_one if x > self.indicator_threshold else self.llr_zero
        if self.llr >= self.upper:
            return True
        if self.llr <= self.lower:
            self.settled = True
        return False

    def _reset_state(self) -> None:
        self.llr = 0.0
        self.settled = False


# ---------------------------------------------------------------------------
# Anytime-valid confidence sequence (betting)
# ---------------------------------------------------------------------------


class ConfidenceSequenceDetector(Detector):
    """Alarm when a one-sided betting confidence sequence excludes ``mu0 + margin``.

    Construction: Waudby-Smith, I. and Ramdas, A. (2023), "Estimating means of
    bounded random variables by betting", JRSS-B 86(1), Sections 3-5. For
    ``X_i`` in ``[0, 1]`` and a candidate mean ``m``, the "+" capital process

        ``K_t(m) = prod_{i <= t} (1 + lambda_i(m) (X_i - m))``

    with *predictable* bets ``lambda_i(m)`` in ``[0, c/m]`` (``c < 1``) is a
    nonnegative supermartingale whenever ``E[X_i | past] <= m``, because each
    factor has conditional mean ``1 + lambda_i (mu - m) <= 1``. By Ville's
    inequality ``P(exists t: K_t(m) >= 1/alpha) <= alpha``. The lower
    confidence sequence is ``{m : K_s(m) < 1/alpha for all s <= t}``; this
    detector tests the single point ``m = mu0 + margin`` and alarms the first
    time ``K_t(m) >= 1/alpha``, i.e. the first time ``m`` falls below the
    sequence. Hence, for any stream whose conditional mean stays at or below
    ``m``, ``P(ever alarm) <= alpha`` at *any* stopping time, with no horizon.

    Betting strategies (both predictable, both from the paper):

    - ``"agrapa"`` (default; Section 5.3 / Appendix, approximate GRAPA):
      ``lambda_t = (mu_hat - m) / (sigma2_hat + (mu_hat - m)^2)``, clipped to
      ``[0, c/m]``. Approximately growth-rate optimal, so it detects a real
      shift quickly.
    - ``"prpl"`` (predictable plug-in, Section 5.1, one-sided version):
      ``lambda_t = sqrt(2 log(1/alpha) / (sigma2_hat t log(1 + t)))``, clipped
      to ``[0, c/m]``.

    with the paper's regularised running estimates
    ``mu_hat_t = (1/2 + sum_{i<=t} X_i) / (t + 1)`` and
    ``sigma2_hat_t = (1/4 + sum_{i<=t} (X_i - mu_hat_i)^2) / (t + 1)``; the bet
    at time ``t`` uses the estimates through ``t - 1``. ``c = 1/2`` follows
    the paper's default truncation.

    The guarantee is about the true A/A mean: when ``mu0`` is a plug-in
    estimate, ``margin`` must absorb its estimation error.
    """

    def __init__(
        self,
        mu0: float,
        margin: float,
        *,
        alpha: float = 0.05,
        betting: str = "agrapa",
        truncation: float = 0.5,
    ) -> None:
        super().__init__()
        if not (math.isfinite(mu0) and math.isfinite(margin)) or margin < 0.0:
            raise ValueError("CS needs finite mu0 and margin >= 0")
        if not 0.0 < alpha < 1.0 or not 0.0 < truncation < 1.0:
            raise ValueError("CS needs 0 < alpha < 1 and 0 < truncation < 1")
        if betting not in {"agrapa", "prpl"}:
            raise ValueError(f"unknown betting strategy {betting!r}")
        self.mu0 = float(mu0)
        self.margin = float(margin)
        self.null_mean = self.mu0 + self.margin
        if self.null_mean <= 0.0:
            raise ValueError("mu0 + margin must be positive")
        self.alpha = float(alpha)
        self.betting = betting
        self.truncation = float(truncation)
        self.log_threshold = math.log(1.0 / alpha)
        self.max_bet = truncation / self.null_mean
        self.log_capital = 0.0
        self._sum_x = 0.0
        self._sum_sq = 0.0

    @classmethod
    def calibrate(
        cls,
        aa_values: Sequence[float],
        *,
        margin: float,
        alpha: float = 0.05,
        betting: str = "agrapa",
    ) -> ConfidenceSequenceDetector:
        """``mu0`` is the A/A mean; alarm once the mean is shown above ``mu0 + margin``."""
        return cls(_mean(_aa_values(aa_values)), margin, alpha=alpha, betting=betting)

    def bet(self) -> float:
        """The predictable bet for the next value (uses only past values)."""
        return self._bet(self._n)

    def _bet(self, folded: int) -> float:
        mu_hat = (0.5 + self._sum_x) / (folded + 1)
        sigma2_hat = (0.25 + self._sum_sq) / (folded + 1)
        if self.betting == "agrapa":
            gap = mu_hat - self.null_mean
            raw = gap / (sigma2_hat + gap * gap)
        else:
            t = folded + 1
            raw = math.sqrt(2.0 * self.log_threshold / (sigma2_hat * t * math.log(1.0 + t)))
        return min(max(raw, 0.0), self.max_bet)

    def _step(self, x: float, t: float) -> bool:
        # Detector.update has already counted x, so the bet uses n - 1 values.
        self.log_capital += math.log1p(self._bet(self._n - 1) * (x - self.null_mean))
        self._sum_x += x
        mu_hat = (0.5 + self._sum_x) / (self._n + 1)
        self._sum_sq += (x - mu_hat) ** 2
        return self.log_capital >= self.log_threshold

    def _reset_state(self) -> None:
        self.log_capital = 0.0
        self._sum_x = 0.0
        self._sum_sq = 0.0


# ---------------------------------------------------------------------------
# Emulations of Agentc's existing guards
# ---------------------------------------------------------------------------

#: ``PLAN_EXPOSURE_WINDOW_US`` in crates/agentc-optimizer/src/plan_guard.rs:23, in seconds.
PLAN_EXPOSURE_WINDOW_S = 24.0 * 60.0 * 60.0
#: ``DEFAULT_PLAN_EXPOSURE_BUDGET`` in plan_guard.rs:25.
DEFAULT_PLAN_EXPOSURE_BUDGET = 1.0
#: Fallback plan threshold in ``Optimizer::divergence_threshold_for_plan``
#: (crates/agentc-optimizer/src/planner.rs:316-329) when no global threshold or
#: rule accuracy budget applies.
DEFAULT_PLAN_DIVERGENCE_THRESHOLD = 0.05
#: ``BREACH_STREAK`` in crates/agentc-optimizer/src/budget.rs:33.
BREACH_STREAK = 5


class ExposureGuard(Detector):
    """Emulation of the complete-plan divergence-exposure guard.

    Mirrors ``PlanGuard::record_sample`` in
    crates/agentc-optimizer/src/plan_guard.rs:272-376 and ``prune_expired`` /
    ``exposure_sum`` at plan_guard.rs:877-889, the contract in predecessor
    protocol Section 11 (``E_t = sum(max(0, divergence_i - threshold))``,
    disable when ``E_t >= 1.0`` within 24 hours):

    - before folding a sample observed at ``t``, drop retained samples with
      ``observed_at <= t - window`` (retain is ``observed_at > cutoff``);
    - ``excess = max(0, divergence - threshold)``; only ``excess > 0`` is
      retained;
    - ``E`` is re-summed over retained samples in insertion order;
    - disable when ``E >= budget``.

    ``threshold`` is the plan's divergence threshold: the configured global
    threshold, else the minimum accuracy budget of the plan's rules, else
    0.05 (planner.rs:316-329). Cooldown, re-admission, duplicate-sequence and
    validator-rejection paths are out of scope: the emulation latches on the
    first disable. Time ``t`` is in seconds; without ``t`` the 1-based value
    index is the clock.
    """

    def __init__(
        self,
        threshold: float = DEFAULT_PLAN_DIVERGENCE_THRESHOLD,
        *,
        budget: float = DEFAULT_PLAN_EXPOSURE_BUDGET,
        window: float = PLAN_EXPOSURE_WINDOW_S,
    ) -> None:
        super().__init__()
        _unit(threshold)
        if not math.isfinite(budget) or budget < 0.0 or not window > 0.0:
            raise ValueError("exposure budget must be finite and >= 0; window > 0")
        self.threshold = float(threshold)
        self.budget = float(budget)
        self.window = float(window)
        self.samples: deque[tuple[float, float]] = deque()
        self.exposure = 0.0

    def _step(self, x: float, t: float) -> bool:
        cutoff = t - self.window
        while self.samples and not self.samples[0][0] > cutoff:
            self.samples.popleft()
        excess = max(x - self.threshold, 0.0)
        if excess > 0.0:
            self.samples.append((t, excess))
        self.exposure = sum(e for _, e in self.samples)
        return self.exposure >= self.budget

    def _reset_state(self) -> None:
        self.samples.clear()
        self.exposure = 0.0


def _f32(x: float) -> float:
    """Round to IEEE single precision, as the Rust controller stores ``f32``."""
    value: float = struct.unpack("f", struct.pack("f", x))[0]
    return value


class ConsecutiveBreachGuard(Detector):
    """Emulation of the per-rule five-consecutive-breach controller.

    Mirrors ``Budget::record_sample`` in
    crates/agentc-optimizer/src/budget.rs:236-312: divergence and budget are
    ``f32``; a sample with ``divergence > budget`` increments the streak, any
    other sample resets it to zero, and the rule is disabled when the streak
    reaches ``BREACH_STREAK = 5`` (budget.rs:33). The 24-hour cooldown is out
    of scope (the emulation latches). Rust rejects out-of-range inputs without
    touching the streak; here they raise ``ValueError`` like every detector.

    ``budget`` is the rule's accuracy budget (e.g. ``ModelDowngrade`` 0.03,
    ``ContextCompress`` 0.02); the study default is the planner fallback 0.05.
    """

    def __init__(
        self,
        budget: float = DEFAULT_PLAN_DIVERGENCE_THRESHOLD,
        *,
        streak: int = BREACH_STREAK,
    ) -> None:
        super().__init__()
        _unit(budget)
        if streak < 1:
            raise ValueError("streak must be >= 1")
        self.budget = float(budget)
        self.streak = int(streak)
        self.consecutive = 0

    def _step(self, x: float, t: float) -> bool:
        if _f32(x) > _f32(self.budget):
            self.consecutive += 1
            return self.consecutive >= self.streak
        self.consecutive = 0
        return False

    def _reset_state(self) -> None:
        self.consecutive = 0


__all__ = [
    "BREACH_STREAK",
    "DEFAULT_PLAN_DIVERGENCE_THRESHOLD",
    "DEFAULT_PLAN_EXPOSURE_BUDGET",
    "PLAN_EXPOSURE_WINDOW_S",
    "ConfidenceSequenceDetector",
    "ConsecutiveBreachGuard",
    "CusumDetector",
    "Detector",
    "ExposureGuard",
    "SprtDetector",
]
