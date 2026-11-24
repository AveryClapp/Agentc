from __future__ import annotations

import json
import random

import numpy as np
import pytest

from agentc.change_safety.detectors import (
    ConfidenceSequenceDetector,
    ConsecutiveBreachGuard,
    CusumDetector,
    Detector,
    ExposureGuard,
    SprtDetector,
)
from bench.change_safety.replay import default_detector_specs, first_alarms, replay, replay_cell
from tests.change_safety.conftest import build_records

DETECTORS: list[Detector] = [
    CusumDetector(mu0=0.125, k=0.0625, h=0.75),
    SprtDetector(0.1, 0.3),
    ConfidenceSequenceDetector(0.25, 0.0625),
    ConfidenceSequenceDetector(0.25, 0.0625, betting="prpl"),
    ConsecutiveBreachGuard(0.25, streak=3),
    ExposureGuard(0.25, window=7.0),
]


@pytest.mark.parametrize("detector", DETECTORS, ids=lambda d: type(d).__name__)
def test_batch_kernels_match_scalar_detectors(detector: Detector) -> None:
    rng = random.Random(4)
    rows, width = 300, 60
    values = np.zeros((rows, width))
    times = np.zeros((rows, width))
    valid = np.zeros((rows, width), dtype=bool)
    expected = []
    for r in range(rows):
        n = rng.randint(0, width)
        level = rng.choice([1, 3, 6])  # dyadic values keep arithmetic exact
        xs = [min(rng.randint(0, level), 8) / 8.0 for _ in range(n)]
        ts = sorted(rng.sample(range(1, 400), n))
        values[r, :n] = xs
        times[r, :n] = ts
        valid[r, :n] = True
        detector.reset()
        hit = -1
        for i, (x, t) in enumerate(zip(xs, ts)):
            if detector.update(x, t=float(t)):
                hit = i
                break
        expected.append(hit)
    detector.reset()
    got = first_alarms(detector, values, valid, times)
    assert got.tolist() == expected
    assert any(e >= 0 for e in expected) and any(e < 0 for e in expected)


def _records():  # type: ignore[no-untyped-def]
    return build_records(
        {"X1": (1.0, 0.6, 0.9, 1.0), "B1": (0.1, 0.0, 0.04, 0.0)},
        n_tasks=20,
        reps=2,
        calls=10,
    )


def test_replay_is_deterministic_and_serializable() -> None:
    records = _records()
    first = replay(records, n_orders=200, n_boot=200, rates=(0.1, 1.0), seed=9)
    second = replay(records, n_orders=200, n_boot=200, rates=(0.1, 1.0), seed=9)
    assert json.dumps(first, sort_keys=True, allow_nan=False) == json.dumps(second, sort_keys=True, allow_nan=False)
    other = replay(records, n_orders=200, n_boot=200, rates=(0.1,), seed=10)
    assert other["cells"]["tau2|m|X1"]["rates"]["0.1"] != first["cells"]["tau2|m|X1"]["rates"]["0.1"]


def test_loud_harm_is_caught_and_aa_is_not_disabled() -> None:
    result = replay(_records(), n_orders=500, n_boot=500, rates=(0.1, 1.0), seed=1)
    x1 = result["cells"]["tau2|m|X1"]
    assert x1["label"] == "harmful"
    # damage 0.75 per task over 12 eligible calls per changed trajectory
    assert x1["delta_per_call"] == pytest.approx(0.75 / 12)
    assert x1["horizon_calls"] == 400  # pool size exceeds floor(5 / delta) = 80
    for name in ("cs[S4]", "sprt[S4]", "cusum[S4]", "cs[S1]", "exposure_guard[S1]", "consecutive_breach[S1]"):
        assert x1["rates"]["1"][name]["catch_rate_in_budget"] == 1.0
        assert x1["rates"]["1"][name]["damage_at_disable"]["p99"] <= 5.0
    assert x1["rates"]["0.1"]["cs[S4]"]["catch_rate_in_budget"] > 0.9
    aa = result["cells"]["tau2|m|C0"]
    assert aa["label"] == "aa"
    for name in ("cs[S4]", "cs[S1]", "sprt[S4]", "exposure_guard[S1]", "consecutive_breach[S1]"):
        assert aa["rates"]["1"][name]["alarm_rate"] <= 0.05
    assert result["config"]["detectors_calibrated_in_sample"] is True
    summary = result["detectors"]["cs[S4]"]["1"]
    assert summary["catch_harmful_mean"] == 1.0 and summary["h5_pass"] is True


def test_replay_counts_calls_and_samples() -> None:
    pool = {"S4": np.ones(50)}
    spec = default_detector_specs({"S4": [0.0] * 50})
    sprt = [s for s in spec if s.name == "sprt[S4]"]
    out = replay_cell(pool, sprt, rate=1.0, n_orders=20, horizon=100, delta=0.01, seed=0, fork_cost_usd=0.5)
    entry = out["sprt[S4]"]
    samples = entry["samples_to_disable"]["p50"]
    assert entry["alarm_rate"] == 1.0
    assert entry["calls_to_disable"]["p50"] == samples  # every call sampled
    assert entry["dollars_to_disable"]["p50"] == pytest.approx(0.5 * samples)
    assert entry["damage_at_disable"]["p50"] == pytest.approx(0.01 * samples)
    assert entry["catch_rate_in_budget"] == 1.0
