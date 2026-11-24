"""Trace replay of online detectors over recorded fork streams (protocol Section 7.3).

For each cell ``(workload, model, arm)`` the recorded forks form a pool of
calls. One *replay order* deploys the change on a sequence of eligible calls
built from independent random permutations of that pool (cycled when the
horizon is longer than the pool). Each call is forked with probability
``rate``; the detector sees only forked calls whose signal is not ``None``.

Damage accrues on every changed call, sampled or not, under the predecessor's
Section 11 contract ``D_t = sum max(0, q_baseline - q_changed)``. With
per-call outcomes unobservable, the cell's measured damage rate is used:
``delta = max(0, cell damage) / mean eligible calls per changed trajectory``
task-equivalent losses per call, so ``D_t = t * delta``. A harmful cell is
*caught in budget* when the detector alarms at call ``t`` with
``D_t <= D_max`` (default 5.0). The horizon is ``horizon_calls`` (default:
the cell's pool size) and, for damaging cells, at least
``floor(D_max / delta)`` so the budget decision is always observed.

False disables are alarms within the horizon on C0 (A/A) cells and on cells
labelled benign by :mod:`bench.change_safety.analysis`.

Detectors run vectorised over replay orders (:func:`first_alarms`); each
batch kernel reproduces the scalar recursion of the corresponding class in
:mod:`agentc.change_safety.detectors` (tests check them against each other).
Everything is deterministic given ``seed``.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from agentc.change_safety.detectors import (
    ConfidenceSequenceDetector,
    ConsecutiveBreachGuard,
    CusumDetector,
    Detector,
    ExposureGuard,
    SprtDetector,
)
from agentc.change_safety.records import SIGNALS, ForkRecord, TrajectoryRecord
from bench.change_safety.analysis import (
    AA_ARM,
    DEFAULT_MARGIN,
    DEFAULT_SEED,
    Analysis,
    analyze,
    cell_key,
    stable_seed,
)

DEFAULT_RATES = (0.01, 0.02, 0.05, 0.10, 1.0)
DEFAULT_N_ORDERS = 10_000
D_MAX = 5.0
#: Rewritten-call traffic used to turn calls into wall time (and the
#: exposure guard's 24 h window into calls). An assumption, reported.
DEFAULT_CALLS_PER_HOUR = 1000.0

Group = tuple[str, str]
Cell = tuple[str, str, str]


@dataclass(frozen=True)
class DetectorSpec:
    """A calibrated detector prototype and the signal it consumes."""

    name: str
    signal: str
    detector: Detector


def default_detector_specs(
    aa_values: Mapping[str, Sequence[float]],
    *,
    cusum_horizon: int = 200,
    target_false_alarm: float = 0.05,
    sprt_delta: float = 0.10,
    cs_margin: float = 0.05,
    existing_threshold: float = 0.05,
    seed: int = DEFAULT_SEED,
) -> list[DetectorSpec]:
    """Protocol Section 6.2 detectors calibrated on one group's A/A values.

    The two existing guards consume S1, the runtime's default ``lexical``
    divergence mode, with the planner's fallback threshold 0.05. CUSUM is
    calibrated to ``target_false_alarm`` within ``cusum_horizon`` sampled
    calls. A detector whose signal has no A/A values is omitted.
    """
    specs = [
        DetectorSpec("exposure_guard[S1]", "S1", ExposureGuard(existing_threshold)),
        DetectorSpec("consecutive_breach[S1]", "S1", ConsecutiveBreachGuard(existing_threshold)),
    ]
    for signal in ("S4", "S1"):
        values = aa_values.get(signal) or []
        if not values:
            continue
        specs.append(
            DetectorSpec(
                f"cusum[{signal}]",
                signal,
                CusumDetector.calibrate(
                    values,
                    horizon=cusum_horizon,
                    target_false_alarm=target_false_alarm,
                    seed=stable_seed(seed, "cusum", signal) % (2**32),
                ),
            )
        )
        specs.append(
            DetectorSpec(
                f"cs[{signal}]",
                signal,
                ConfidenceSequenceDetector.calibrate(values, margin=cs_margin, alpha=target_false_alarm),
            )
        )
    s4 = aa_values.get("S4") or []
    if s4:
        specs.append(DetectorSpec("sprt[S4]", "S4", SprtDetector.calibrate(s4, delta=sprt_delta)))
    return specs


# ---------------------------------------------------------------------------
# Batch kernels
# ---------------------------------------------------------------------------


def _first(alarm: np.ndarray) -> np.ndarray:
    has = alarm.any(axis=1)
    out: np.ndarray = np.where(has, alarm.argmax(axis=1), -1)
    return out


def _shifted_cumsum(a: np.ndarray) -> np.ndarray:
    c = np.cumsum(a, axis=1)
    out: np.ndarray = np.concatenate([np.zeros((a.shape[0], 1)), c[:, :-1]], axis=1)
    return out


def first_alarms(
    detector: Detector,
    values: np.ndarray,
    valid: np.ndarray,
    times: np.ndarray,
) -> np.ndarray:
    """First alarming column per row, or -1.

    ``values`` is ``(rows, m)`` with each row's valid entries left-packed
    (``valid`` marks them); ``times`` are the entries' clock values (used only
    by :class:`ExposureGuard`). The detector's parameters are read, its state
    is not touched.
    """
    v = np.where(valid, values, 0.0)
    if isinstance(detector, CusumDetector):
        inc = np.where(valid, v - detector.mu0 - detector.k, 0.0)
        c = np.cumsum(inc, axis=1)
        stat = c - np.minimum(np.minimum.accumulate(c, axis=1), 0.0)
        return _first(valid & (stat > detector.h))
    if isinstance(detector, SprtDetector):
        inc = np.where(
            valid,
            np.where(v > detector.indicator_threshold, detector.llr_one, detector.llr_zero),
            0.0,
        )
        llr = np.cumsum(inc, axis=1)
        up = _first(valid & (llr >= detector.upper))
        down = _first(valid & (llr <= detector.lower))
        out: np.ndarray = np.where((up >= 0) & ((down < 0) | (up < down)), up, -1)
        return out
    if isinstance(detector, ConfidenceSequenceDetector):
        m = detector.null_mean
        j = np.arange(v.shape[1], dtype=float)[None, :]
        sx_prev = _shifted_cumsum(v)
        mu_after = (0.5 + sx_prev + v) / (j + 2.0)
        sq = np.where(valid, (v - mu_after) ** 2, 0.0)
        ssq_prev = _shifted_cumsum(sq)
        mu_hat = (0.5 + sx_prev) / (j + 1.0)
        sigma2 = (0.25 + ssq_prev) / (j + 1.0)
        if detector.betting == "agrapa":
            gap = mu_hat - m
            raw = gap / (sigma2 + gap * gap)
        else:
            t = j + 1.0
            raw = np.sqrt(2.0 * detector.log_threshold / (sigma2 * t * np.log1p(t)))
        bet = np.clip(raw, 0.0, detector.max_bet)
        logk = np.cumsum(np.where(valid, np.log1p(bet * (v - m)), 0.0), axis=1)
        return _first(valid & (logk >= detector.log_threshold))
    if isinstance(detector, ConsecutiveBreachGuard):
        breach = valid & (v.astype(np.float32) > np.float32(detector.budget))
        idx = np.arange(v.shape[1])[None, :]
        last_reset = np.maximum.accumulate(np.where(breach, -1, idx), axis=1)
        return _first(breach & (idx - last_reset >= detector.streak))
    if isinstance(detector, ExposureGuard):
        excess = np.where(valid, np.maximum(v - detector.threshold, 0.0), 0.0)
        csum = np.cumsum(excess, axis=1)
        exposure = np.empty_like(csum)
        for r in range(v.shape[0]):
            n = int(valid[r].sum())
            exposure[r, n:] = 0.0
            if n == 0:
                continue
            tr = times[r, :n]
            lo = np.searchsorted(tr, tr - detector.window, side="right")
            before = np.where(lo > 0, csum[r, np.maximum(lo - 1, 0)], 0.0)
            exposure[r, :n] = csum[r, :n] - before
        return _first(valid & (exposure >= detector.budget))
    # Generic fallback: scalar replay of any other Detector.
    result = np.full(v.shape[0], -1)
    for r in range(v.shape[0]):
        detector.reset()
        for col in range(v.shape[1]):
            if not valid[r, col]:
                break
            if detector.update(float(v[r, col]), t=float(times[r, col])):
                result[r] = col
                break
    detector.reset()
    return result


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


def _orders(rng: np.random.Generator, rows: int, pool: int, horizon: int) -> np.ndarray:
    cycles = -(-horizon // pool)
    base = np.broadcast_to(np.arange(pool), (rows, pool))
    blocks = [rng.permuted(base, axis=1) for _ in range(cycles)]
    out: np.ndarray = np.concatenate(blocks, axis=1)[:, :horizon]
    return out


def _quantiles(values: np.ndarray, qs: Sequence[float]) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for q in qs:
        if values.size == 0:
            out[f"p{q:g}"] = None
            continue
        # No interpolation, so an infinite entry (no disable) stays infinite.
        v = float(np.percentile(values, q, method="inverted_cdf"))
        out[f"p{q:g}"] = v if math.isfinite(v) else None
    return out


def replay_cell(
    pool: Mapping[str, np.ndarray],
    specs: Sequence[DetectorSpec],
    *,
    rate: float,
    n_orders: int,
    horizon: int,
    delta: float,
    seed: int,
    fork_cost_usd: float | None = None,
    calls_per_hour: float = DEFAULT_CALLS_PER_HOUR,
    d_max: float = D_MAX,
    chunk_elements: int = 2_000_000,
) -> dict[str, Any]:
    """Replay one cell's call pool at one sampling rate for every detector.

    ``pool`` maps signal name to a 1-D array over the cell's recorded calls
    (NaN where the signal is missing). All detectors share the same orders
    and sampling draws.
    """
    sizes = {v.size for v in pool.values()}
    if len(sizes) != 1 or 0 in sizes:
        raise ValueError("pool arrays must be non-empty and aligned")
    n_pool = sizes.pop()
    rng = np.random.default_rng(seed)
    chunk = max(1, min(n_orders, chunk_elements // max(horizon, 1)))
    calls: dict[str, list[np.ndarray]] = defaultdict(list)
    samples: dict[str, list[np.ndarray]] = defaultdict(list)
    done = 0
    while done < n_orders:
        rows = min(chunk, n_orders - done)
        order = _orders(rng, rows, n_pool, horizon)
        sampled = np.ones((rows, horizon), dtype=bool) if rate >= 1.0 else rng.random((rows, horizon)) < rate
        for spec in specs:
            raw = pool[spec.signal][order]
            ok = sampled & np.isfinite(raw)
            counts = ok.sum(axis=1)
            width = max(int(counts.max()), 1)
            packing = np.argsort(~ok, axis=1, kind="stable")[:, :width]
            values = np.take_along_axis(raw, packing, axis=1)
            valid = np.arange(width)[None, :] < counts[:, None]
            positions = packing  # 0-based call index of each packed sample
            times = (positions + 1) * (3600.0 / calls_per_hour)
            col = first_alarms(spec.detector, values, valid, times)
            hit = col >= 0
            pos = np.where(hit, positions[np.arange(rows), np.maximum(col, 0)], -1)
            calls[spec.name].append(np.where(hit, pos + 1, -1))
            samples[spec.name].append(np.where(hit, col + 1, -1))
        done += rows

    out: dict[str, Any] = {}
    budget_calls = math.floor(d_max / delta) if delta > 0 else None
    for spec in specs:
        c = np.concatenate(calls[spec.name])
        s = np.concatenate(samples[spec.name])
        alarmed = c > 0
        damage = np.where(alarmed, c * delta, np.inf)
        entry: dict[str, Any] = {
            "alarm_rate": float(alarmed.mean()),
            "calls_to_disable": _quantiles(c[alarmed].astype(float), (50, 95)),
            "samples_to_disable": _quantiles(s[alarmed].astype(float), (50, 95)),
            "hours_to_disable": _quantiles(c[alarmed] / calls_per_hour, (50, 95)),
            # None = the percentile falls on replays that never disabled.
            "damage_at_disable": _quantiles(damage, (50, 95, 99)),
        }
        entry["dollars_to_disable"] = (
            _quantiles(s[alarmed] * fork_cost_usd, (50, 95)) if fork_cost_usd is not None else None
        )
        if budget_calls is not None:
            entry["catch_rate_in_budget"] = float((alarmed & (c <= budget_calls)).mean())
        out[spec.name] = entry
    return out


def _cell_pools(forks: Iterable[ForkRecord], applied_only: bool) -> dict[Cell, dict[str, Any]]:
    rows: dict[Cell, list[ForkRecord]] = defaultdict(list)
    for f in forks:
        if applied_only and f.arm != AA_ARM and not f.change_applied:
            continue
        rows[(f.key.workload, f.key.model, f.arm)].append(f)
    pools: dict[Cell, dict[str, Any]] = {}
    for cell, members in rows.items():
        members.sort(key=lambda f: (f.key.task_id, f.key.repetition, f.key.call_index))
        values = {
            name: np.array([np.nan if (x := f.signals.get(name)) is None else x for f in members])
            for name in SIGNALS
        }
        costs = [f.fork.cost_usd for f in members if f.fork is not None and f.fork.cost_usd is not None]
        pools[cell] = {
            "values": values,
            "fork_cost_usd": float(np.mean(costs)) if costs else None,
        }
    return pools


def _aa_values(forks: Iterable[ForkRecord]) -> dict[Group, dict[str, list[float]]]:
    out: dict[Group, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for f in forks:
        if f.arm != AA_ARM:
            continue
        for name in SIGNALS:
            value = f.signals.get(name)
            if value is not None:
                out[(f.key.workload, f.key.model)][name].append(value)
    return out


def replay(
    records: Sequence[ForkRecord | TrajectoryRecord],
    *,
    analysis: Analysis | None = None,
    detectors: Mapping[Group, Sequence[DetectorSpec]] | None = None,
    calibration_records: Sequence[ForkRecord | TrajectoryRecord] | None = None,
    rates: Sequence[float] = DEFAULT_RATES,
    n_orders: int = DEFAULT_N_ORDERS,
    horizon_calls: int | None = None,
    seed: int = DEFAULT_SEED,
    d_max: float = D_MAX,
    margin: float = DEFAULT_MARGIN,
    calls_per_hour: float = DEFAULT_CALLS_PER_HOUR,
    n_boot: int = 10_000,
    applied_only: bool = True,
) -> dict[str, Any]:
    """Protocol Section 7.3 replay of every cell, detector and sampling rate.

    Cell labels and damage come from ``analysis`` (computed with ``margin``
    and ``n_boot`` when omitted). Detectors are ``detectors[group]`` when
    given, else :func:`default_detector_specs` calibrated on the C0 forks of
    ``calibration_records`` (protocol: calibration split only), else on the
    C0 forks of ``records`` themselves, which the report flags as in-sample.
    """
    records = list(records)
    forks = [r for r in records if isinstance(r, ForkRecord)]
    if analysis is None:
        analysis = analyze(records, margin=margin, n_boot=n_boot, n_perm=0, seed=seed, applied_only=applied_only)
    in_sample = detectors is None and calibration_records is None
    if detectors is None:
        source = forks if calibration_records is None else [
            r for r in calibration_records if isinstance(r, ForkRecord)
        ]
        detectors = {g: default_detector_specs(v, seed=seed) for g, v in _aa_values(source).items()}

    pools = _cell_pools(forks, applied_only)
    cells_out: dict[str, Any] = {}
    summary: dict[str, dict[str, dict[str, list[float]]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for cell in sorted(pools):
        group = (cell[0], cell[1])
        specs = list(detectors.get(group, ()))
        if not specs:
            continue
        stats = analysis.cells.get(cell)
        if cell[2] == AA_ARM:
            label, delta = "aa", 0.0
        elif stats is None or not math.isfinite(stats.damage):
            continue
        else:
            label = stats.label
            eligible = stats.report.get("mean_eligible_calls")
            delta = max(0.0, stats.damage) / eligible if eligible else 0.0
        pool = pools[cell]
        n_pool = len(next(iter(pool["values"].values())))
        horizon = horizon_calls if horizon_calls is not None else n_pool
        if delta > 0:
            horizon = max(horizon, math.floor(d_max / delta))
        per_rate: dict[str, Any] = {}
        for rate in rates:
            result = replay_cell(
                pool["values"],
                specs,
                rate=rate,
                n_orders=n_orders,
                horizon=horizon,
                delta=delta,
                seed=stable_seed(seed, *cell, rate) % (2**63),
                fork_cost_usd=pool["fork_cost_usd"],
                calls_per_hour=calls_per_hour,
                d_max=d_max,
            )
            per_rate[f"{rate:g}"] = result
            for name, entry in result.items():
                if label == "harmful":
                    summary[name][f"{rate:g}"]["catch_harmful"].append(entry["catch_rate_in_budget"])
                elif label in {"aa", "benign"}:
                    summary[name][f"{rate:g}"][f"false_disable_{label}"].append(entry["alarm_rate"])
        cells_out[cell_key(cell)] = {
            "label": label,
            "delta_per_call": delta,
            "horizon_calls": horizon,
            "pool_calls": n_pool,
            "rates": per_rate,
        }

    detectors_out: dict[str, Any] = {}
    for name, by_rate in sorted(summary.items()):
        detectors_out[name] = {}
        for rate_key, parts in by_rate.items():
            agg: dict[str, Any] = {}
            for key, values in parts.items():
                agg[f"{key}_mean"] = float(np.mean(values))
                agg[f"{key}_n_cells"] = len(values)
            catch = agg.get("catch_harmful_mean")
            false = [agg[k] for k in ("false_disable_aa_mean", "false_disable_benign_mean") if k in agg]
            agg["h5_pass"] = (
                None if catch is None or not false else bool(catch >= 0.9 and max(false) <= 0.05)
            )
            detectors_out[name][rate_key] = agg

    return {
        "config": {
            "rates": list(rates),
            "n_orders": n_orders,
            "horizon_calls": horizon_calls,
            "seed": seed,
            "d_max": d_max,
            "calls_per_hour": calls_per_hour,
            "detectors_calibrated_in_sample": in_sample,
            "detector_params": {
                f"{g[0]}|{g[1]}": {s.name: _params(s) for s in specs}
                for g, specs in sorted(detectors.items())
            },
        },
        "cells": cells_out,
        "detectors": detectors_out,
    }


def _params(spec: DetectorSpec) -> dict[str, Any]:
    d = spec.detector
    keys = ("mu0", "k", "h", "p0", "p1", "alpha", "beta", "margin", "betting", "threshold", "budget", "window", "streak")
    out: dict[str, Any] = {"type": type(d).__name__, "signal": spec.signal}
    for key in keys:
        if hasattr(d, key):
            out[key] = getattr(d, key)
    return out


__all__ = [
    "DEFAULT_CALLS_PER_HOUR",
    "DEFAULT_N_ORDERS",
    "DEFAULT_RATES",
    "D_MAX",
    "DetectorSpec",
    "default_detector_specs",
    "first_alarms",
    "replay",
    "replay_cell",
]
