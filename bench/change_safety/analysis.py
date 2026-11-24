"""Offline predictive-validity analysis (protocol Sections 2 and 7.2).

Inputs are the study's :class:`ForkRecord` and :class:`TrajectoryRecord`
streams. A *cell* is ``(workload, model, arm)``; a *group* is
``(workload, model)``. Every interval is a task-cluster percentile bootstrap:
each replicate resamples the tasks of every group with replacement and
recomputes the statistic from per-task sums, so trajectory and fork evidence
for the same task always move together. All statistics in one
:func:`summarize` call share the same replicates, which makes H3's paired
AUROC difference paired.

Conventions:

- Damage is ``baseline reward - changed reward``: repetitions are averaged
  within a task first, then the paired difference is averaged over tasks
  present in both arms.
- A cell is *harmful* when its damage interval's lower bound exceeds
  ``margin``, *benign* when the whole interval lies strictly inside
  ``(-margin, margin)``, otherwise *indeterminate*. Labels come from the full
  data and stay fixed inside bootstraps (only the scores are resampled).
- Excess signal of a cell is its fork-signal mean minus the C0 fork-signal
  mean in the same group. ``exceed_rate`` is the share of the cell's fork
  values above the group's C0 95th percentile (the percentile is the
  full-data point estimate).
- Forks with ``change_applied = False`` are dropped from changed arms (an
  unapplied change sent the unchanged request); C0 forks are always kept.
- ``None`` signals are missing, never zero.
- Permutation p-values are one-sided in the H2 direction (positive rank
  correlation, AUROC above 0.5) and Holm-corrected across the five signals
  within each scope (each workload, and pooled).

Only numpy is used; Spearman, Mann-Whitney AUROC and Holm are implemented
here.
"""

from __future__ import annotations

import hashlib
import math
import warnings
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from agentc.change_safety.records import (
    SIGNALS,
    ForkRecord,
    TrajectoryRecord,
)

AA_ARM = "C0"
BASELINE_ARM = "baseline"
DEFAULT_MARGIN = 0.05
DEFAULT_N_BOOT = 10_000
DEFAULT_SEED = 20261109
TEXT_SIGNALS = ("S1", "S2", "S3")

#: Change families of the protocol Section 4 catalog. Pass ``families`` to
#: :func:`summarize` for any other change identifier.
DEFAULT_FAMILIES: Mapping[str, str] = {
    "C0": "aa_control",
    "C1": "model_swap",
    "C2": "model_migration",
    "C3": "context_removal",
    "C4": "context_removal",
    "C5": "context_removal",
    "C6": "output_cap",
    "X1": "seeded_harm",
    "X2": "seeded_harm",
    "X3": "seeded_harm",
    "B1": "benign",
    "B2": "benign",
}

Group = tuple[str, str]
Cell = tuple[str, str, str]


# ---------------------------------------------------------------------------
# Small statistics
# ---------------------------------------------------------------------------


def stable_seed(*parts: object) -> int:
    """Process-independent 64-bit seed from arbitrary parts."""
    digest = hashlib.sha256(repr(parts).encode()).digest()
    return int.from_bytes(digest[:8], "big")


def rankdata(values: np.ndarray) -> np.ndarray:
    """Average ranks (1-based) along the last axis, ties sharing their mean rank."""
    a = np.asarray(values, dtype=float)
    less = (a[..., None, :] < a[..., :, None]).sum(axis=-1)
    equal = (a[..., None, :] == a[..., :, None]).sum(axis=-1)
    result: np.ndarray = less + (equal + 1) / 2.0
    return result


def _pearson_rows(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    xc = x - x.mean(axis=-1, keepdims=True)
    yc = y - y.mean(axis=-1, keepdims=True)
    den = np.sqrt((xc * xc).sum(axis=-1) * (yc * yc).sum(axis=-1))
    with np.errstate(invalid="ignore", divide="ignore"):
        out: np.ndarray = np.where(den > 0, (xc * yc).sum(axis=-1) / den, np.nan)
    return out


def spearman(x: Sequence[float] | np.ndarray, y: Sequence[float] | np.ndarray) -> np.ndarray | float:
    """Spearman rank correlation along the last axis (Pearson on average ranks).

    Rows containing NaN yield NaN; constant rows yield NaN.
    """
    xa = np.asarray(x, dtype=float)
    ya = np.asarray(y, dtype=float)
    xa, ya = np.broadcast_arrays(xa, ya)
    bad = np.isnan(xa).any(axis=-1) | np.isnan(ya).any(axis=-1)
    rho = _pearson_rows(rankdata(xa), rankdata(ya))
    rho = np.where(bad, np.nan, rho)
    return float(rho) if rho.ndim == 0 else rho


def auroc(scores: Sequence[float] | np.ndarray, labels: Sequence[int] | np.ndarray) -> np.ndarray | float:
    """Mann-Whitney AUROC of ``scores`` for ``labels == 1`` vs ``labels == 0``.

    Ties count one half. Works along the last axis of ``scores``; rows
    containing NaN yield NaN.
    """
    s = np.asarray(scores, dtype=float)
    lab = np.asarray(labels).astype(bool)
    lab = np.broadcast_to(lab, s.shape)
    n1 = lab.sum(axis=-1)
    n0 = lab.shape[-1] - n1
    ranks = rankdata(s)
    with np.errstate(invalid="ignore", divide="ignore"):
        u = (ranks * lab).sum(axis=-1) - n1 * (n1 + 1) / 2.0
        out = np.where((n1 > 0) & (n0 > 0), u / (n1 * n0), np.nan)
    out = np.where(np.isnan(s).any(axis=-1), np.nan, out)
    return float(out) if out.ndim == 0 else out


def holm(pvalues: Sequence[float | None], alpha: float = 0.05) -> list[dict[str, Any]]:
    """Holm step-down adjustment. ``None``/NaN p-values are passed through."""
    indexed = [(p, i) for i, p in enumerate(pvalues) if p is not None and not math.isnan(p)]
    indexed.sort()
    m = len(indexed)
    adjusted: dict[int, float] = {}
    running = 0.0
    for rank, (p, i) in enumerate(indexed):
        running = max(running, min(1.0, (m - rank) * p))
        adjusted[i] = running
    return [
        {
            "p": pvalues[i],
            "p_holm": adjusted.get(i),
            "reject": adjusted.get(i) is not None and adjusted[i] <= alpha,
        }
        for i in range(len(pvalues))
    ]


def _interval(point: float, reps: np.ndarray) -> dict[str, float | None]:
    finite = reps[np.isfinite(reps)]
    if finite.size == 0:
        lo = hi = math.nan
    else:
        lo, hi = (float(v) for v in np.percentile(finite, [2.5, 97.5]))
    return {"point": _num(point), "lo": _num(lo), "hi": _num(hi)}


def _nanmean_rows(stacked: np.ndarray) -> np.ndarray:
    """Column-wise mean over cells ignoring NaN; all-NaN columns stay NaN quietly."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        out: np.ndarray = np.nanmean(stacked, axis=0)
    return out


def _num(value: float | None) -> float | None:
    if value is None:
        return None
    v = float(value)
    return v if math.isfinite(v) else None


def _ratio(num: np.ndarray, den: np.ndarray) -> np.ndarray:
    with np.errstate(invalid="ignore", divide="ignore"):
        out: np.ndarray = np.where(den > 0, num / np.where(den > 0, den, 1.0), np.nan)
    return out


def classify(lo: float | None, hi: float | None, margin: float) -> str:
    """Protocol Section 7.2 cell label from a damage interval."""
    if lo is None or hi is None:
        return "indeterminate"
    if lo > margin:
        return "harmful"
    if -margin < lo and hi < margin:
        return "benign"
    return "indeterminate"


def cluster_bootstrap_weights(n_clusters: int, n_boot: int, rng: np.random.Generator) -> np.ndarray:
    """``(n_boot, n_clusters)`` multiplicities of a cluster bootstrap."""
    draws = rng.integers(0, n_clusters, size=(n_boot, n_clusters))
    weights = np.zeros((n_boot, n_clusters))
    np.add.at(weights, (np.arange(n_boot)[:, None], draws), 1.0)
    return weights


# ---------------------------------------------------------------------------
# Per-task aggregation
# ---------------------------------------------------------------------------


@dataclass
class _TaskSums:
    """Per-task numerators and denominators aligned to a group's task list."""

    num: np.ndarray
    den: np.ndarray

    def point(self) -> float:
        total = float(self.den.sum())
        return float(self.num.sum()) / total if total > 0 else math.nan

    def reps(self, weights: np.ndarray) -> np.ndarray:
        return _ratio(weights @ self.num, weights @ self.den)


@dataclass
class _GroupData:
    tasks: list[str]
    weights: np.ndarray
    #: arm -> task -> list of trajectories
    traj: dict[str, dict[str, list[TrajectoryRecord]]] = field(default_factory=dict)
    #: arm -> signal -> task -> values
    forks: dict[str, dict[str, dict[str, list[float]]]] = field(default_factory=dict)
    fork_counts: dict[str, dict[str, int]] = field(default_factory=dict)

    def _vector(self, values: Mapping[str, float]) -> tuple[np.ndarray, np.ndarray]:
        num = np.zeros(len(self.tasks))
        den = np.zeros(len(self.tasks))
        for i, task in enumerate(self.tasks):
            if task in values:
                num[i] = values[task]
                den[i] = 1.0
        return num, den

    def traj_task_means(self, arm: str, attribute: str) -> dict[str, float]:
        return {
            task: float(np.mean([float(getattr(t, attribute)) for t in rows]))
            for task, rows in self.traj.get(arm, {}).items()
            if rows
        }

    def paired_difference(self, arm: str, attribute: str, sign: float) -> _TaskSums:
        """Per-task ``sign * (baseline - arm)`` over tasks with both arms."""
        base = self.traj_task_means(BASELINE_ARM, attribute)
        changed = self.traj_task_means(arm, attribute)
        diffs = {t: sign * (base[t] - changed[t]) for t in base.keys() & changed.keys()}
        num, den = self._vector(diffs)
        return _TaskSums(num, den)

    def fork_mean(self, arm: str, signal: str, transform: Any = None) -> _TaskSums:
        num = np.zeros(len(self.tasks))
        den = np.zeros(len(self.tasks))
        by_task = self.forks.get(arm, {}).get(signal, {})
        for i, task in enumerate(self.tasks):
            values = by_task.get(task)
            if values:
                arr = np.asarray(values, dtype=float)
                if transform is not None:
                    arr = transform(arr)
                num[i] = arr.sum()
                den[i] = arr.size
        return _TaskSums(num, den)

    def all_fork_values(self, arm: str, signal: str) -> np.ndarray:
        by_task = self.forks.get(arm, {}).get(signal, {})
        chunks = [np.asarray(v, dtype=float) for v in by_task.values() if v]
        return np.concatenate(chunks) if chunks else np.zeros(0)


def _split(records: Iterable[ForkRecord | TrajectoryRecord], stage: str | None) -> tuple[list[ForkRecord], list[TrajectoryRecord]]:
    forks: list[ForkRecord] = []
    trajs: list[TrajectoryRecord] = []
    for record in records:
        if isinstance(record, ForkRecord):
            if stage is None or record.key.stage == stage:
                forks.append(record)
        elif isinstance(record, TrajectoryRecord):
            if stage is None or record.stage == stage:
                trajs.append(record)
        else:
            raise TypeError(f"unexpected record {type(record).__name__}")
    return forks, trajs


def _build_groups(
    forks: Sequence[ForkRecord],
    trajs: Sequence[TrajectoryRecord],
    n_boot: int,
    seed: int,
    applied_only: bool,
) -> dict[Group, _GroupData]:
    tasks: dict[Group, set[str]] = defaultdict(set)
    for f in forks:
        tasks[(f.key.workload, f.key.model)].add(f.key.task_id)
    for t in trajs:
        tasks[(t.workload, t.model)].add(t.task_id)
    groups: dict[Group, _GroupData] = {}
    for group in sorted(tasks):
        ordered = sorted(tasks[group])
        rng = np.random.default_rng(stable_seed(seed, *group))
        groups[group] = _GroupData(ordered, cluster_bootstrap_weights(len(ordered), n_boot, rng))
    for t in trajs:
        g = groups[(t.workload, t.model)]
        g.traj.setdefault(t.arm, {}).setdefault(t.task_id, []).append(t)
    for f in forks:
        g = groups[(f.key.workload, f.key.model)]
        counts = g.fork_counts.setdefault(f.arm, {"total": 0, "used": 0})
        counts["total"] += 1
        if applied_only and f.arm != AA_ARM and not f.change_applied:
            continue
        counts["used"] += 1
        per_signal = g.forks.setdefault(f.arm, {})
        for name in SIGNALS:
            value = f.signals.get(name)
            if value is not None:
                per_signal.setdefault(name, {}).setdefault(f.key.task_id, []).append(value)
    return groups


# ---------------------------------------------------------------------------
# H1: A/A floor
# ---------------------------------------------------------------------------


def _aa_floor(group: _GroupData) -> dict[str, Any]:
    w = group.weights
    out: dict[str, Any] = {"n_forks": group.fork_counts.get(AA_ARM, {}).get("used", 0)}
    s4 = group.fork_mean(AA_ARM, "S4", transform=lambda a: (a > 0).astype(float))
    out["action_disagreement_rate"] = _interval(s4.point(), s4.reps(w))
    signals: dict[str, Any] = {}
    for name in SIGNALS:
        values = group.all_fork_values(AA_ARM, name)
        mean = group.fork_mean(AA_ARM, name)
        entry: dict[str, Any] = {"n": int(values.size), "mean": _interval(mean.point(), mean.reps(w))}
        if values.size:
            for q in (50, 90, 95, 99):
                entry[f"p{q}"] = float(np.percentile(values, q))
            entry["rate_positive"] = float((values > 0).mean())
        signals[name] = entry
    out["signals"] = signals
    return out


# ---------------------------------------------------------------------------
# Cells
# ---------------------------------------------------------------------------


@dataclass
class CellStats:
    """Point estimates and bootstrap replicates for one changed cell."""

    cell: Cell
    family: str | None
    n_tasks: int
    damage: float
    damage_reps: np.ndarray
    label: str
    excess: dict[str, float]
    excess_reps: dict[str, np.ndarray]
    report: dict[str, Any]


def _cell_stats(
    group_key: Group,
    group: _GroupData,
    arm: str,
    margin: float,
    families: Mapping[str, str],
) -> CellStats:
    w = group.weights
    damage = group.paired_difference(arm, "reward", 1.0)
    damage_point = damage.point()
    damage_reps = damage.reps(w)
    damage_iv = _interval(damage_point, damage_reps)
    label = classify(damage_iv["lo"], damage_iv["hi"], margin)
    report: dict[str, Any] = {
        "workload": group_key[0],
        "model": group_key[1],
        "arm": arm,
        "family": families.get(arm),
        "n_tasks_paired": int(damage.den.sum()),
        "damage": damage_iv,
        "label": label,
        "forks": group.fork_counts.get(arm, {"total": 0, "used": 0}),
    }
    for attribute, name in (("model_calls", "added_model_calls"), ("tool_calls", "added_tool_calls")):
        extra = group.paired_difference(arm, attribute, -1.0)
        report[name] = _interval(extra.point(), extra.reps(w))
    eligible = group.traj_task_means(arm, "eligible_calls")
    report["mean_eligible_calls"] = _num(float(np.mean(list(eligible.values())))) if eligible else None

    excess: dict[str, float] = {}
    excess_reps: dict[str, np.ndarray] = {}
    signal_report: dict[str, Any] = {}
    for name in SIGNALS:
        fork = group.fork_mean(arm, name)
        aa = group.fork_mean(AA_ARM, name)
        point = fork.point() - aa.point()
        reps = fork.reps(w) - aa.reps(w)
        excess[name] = point
        excess_reps[name] = reps
        aa_values = group.all_fork_values(AA_ARM, name)
        entry: dict[str, Any] = {
            "fork_mean": _num(fork.point()),
            "aa_mean": _num(aa.point()),
            "excess_mean": _interval(point, reps),
            "n": int(fork.den.sum()),
        }
        if aa_values.size and fork.den.sum() > 0:
            q95 = float(np.percentile(aa_values, 95))
            over = group.fork_mean(arm, name, transform=lambda a, q=q95: (a > q).astype(float))
            entry["aa_p95"] = q95
            entry["exceed_rate"] = _interval(over.point(), over.reps(w))
        signal_report[name] = entry
    report["signals"] = signal_report
    return CellStats(
        cell=(group_key[0], group_key[1], arm),
        family=families.get(arm),
        n_tasks=int(damage.den.sum()),
        damage=damage_point,
        damage_reps=damage_reps,
        label=label,
        excess=excess,
        excess_reps=excess_reps,
        report=report,
    )


def cell_key(cell: Cell) -> str:
    return "|".join(cell)


# ---------------------------------------------------------------------------
# H2 / H3
# ---------------------------------------------------------------------------


def _perm_pvalue(statistic: Any, x: np.ndarray, y: np.ndarray, observed: float, n_perm: int, rng: np.random.Generator) -> float | None:
    if not math.isfinite(observed) or n_perm <= 0:
        return None
    perms = np.argsort(rng.random((n_perm, y.size)), axis=1)
    null = statistic(np.broadcast_to(x, (n_perm, x.size)), y[perms])
    null = np.asarray(null, dtype=float)
    return float((1 + np.sum(null >= observed - 1e-12)) / (1 + n_perm))


def _association(cells: Sequence[CellStats], n_perm: int, seed: int, scope: str) -> dict[str, Any]:
    out: dict[str, Any] = {"n_cells": len(cells)}
    usable = [c for c in cells if math.isfinite(c.damage)]
    damage = np.array([c.damage for c in usable])
    damage_reps = np.stack([c.damage_reps for c in usable], axis=1) if usable else np.zeros((0, 0))
    labelled = [c for c in usable if c.label in {"harmful", "benign"}]
    labels = np.array([1 if c.label == "harmful" else 0 for c in labelled])
    out["n_harmful"] = int(labels.sum())
    out["n_benign"] = int(labels.size - labels.sum())
    spearman_rows: dict[str, Any] = {}
    auroc_rows: dict[str, Any] = {}
    auroc_reps: dict[str, np.ndarray] = {}
    for name in SIGNALS:
        rng = np.random.default_rng(stable_seed(seed, scope, name))
        x = np.array([c.excess[name] for c in usable])
        ok = np.isfinite(x)
        if ok.sum() >= 3:
            rho = float(spearman(x[ok], damage[ok]))
            xr = np.stack([c.excess_reps[name] for c in usable], axis=1)[:, ok]
            reps = np.asarray(spearman(xr, damage_reps[:, ok]), dtype=float)
            entry = _interval(rho, reps)
            entry["n"] = int(ok.sum())
            entry["p_perm"] = _perm_pvalue(spearman, x[ok], damage[ok], rho, n_perm, rng)
        else:
            entry = {"point": None, "lo": None, "hi": None, "n": int(ok.sum()), "p_perm": None}
        spearman_rows[name] = entry

        xs = np.array([c.excess[name] for c in labelled])
        okl = np.isfinite(xs)
        if okl.any() and 0 < labels[okl].sum() < okl.sum():
            area = float(auroc(xs[okl], labels[okl]))
            xr = np.stack([c.excess_reps[name] for c in labelled], axis=1)[:, okl]
            reps = np.asarray(auroc(xr, labels[okl]), dtype=float)
            auroc_reps[name] = reps
            a_entry = _interval(area, reps)
            a_entry["p_perm"] = _perm_pvalue(
                lambda s, lab: auroc(s, lab), xs[okl], labels[okl], area, n_perm, rng
            )
        else:
            a_entry = {"point": None, "lo": None, "hi": None, "p_perm": None}
        auroc_rows[name] = a_entry

    for rows in (spearman_rows, auroc_rows):
        adjusted = holm([rows[name]["p_perm"] for name in SIGNALS])
        for name, adj in zip(SIGNALS, adjusted):
            rows[name]["p_holm"] = adj["p_holm"]
            rows[name]["reject_holm_0.05"] = adj["reject"]
    out["spearman"] = spearman_rows
    out["auroc"] = auroc_rows

    # H3: paired AUROC difference S4 - Sk on the same replicates.
    h3: dict[str, Any] = {}
    base = auroc_rows.get("S4", {}).get("point")
    for name in TEXT_SIGNALS:
        other = auroc_rows.get(name, {}).get("point")
        if base is None or other is None or "S4" not in auroc_reps or name not in auroc_reps:
            h3[f"S4-{name}"] = {"point": None, "lo": None, "hi": None, "p_boot_le0": None}
            continue
        diff = auroc_reps["S4"] - auroc_reps[name]
        entry = _interval(base - other, diff)
        finite = diff[np.isfinite(diff)]
        entry["p_boot_le0"] = float((finite <= 0).mean()) if finite.size else None
        h3[f"S4-{name}"] = entry
    out["h3_auroc_difference"] = h3
    return out


# ---------------------------------------------------------------------------
# H4
# ---------------------------------------------------------------------------


def _family_ratios(cells: Sequence[CellStats]) -> dict[str, Any]:
    by_family: dict[str, list[CellStats]] = defaultdict(list)
    for c in cells:
        if c.family is not None and math.isfinite(c.damage):
            by_family[c.family].append(c)
    out: dict[str, Any] = {}
    for family, members in sorted(by_family.items()):
        damage = float(np.mean([c.damage for c in members]))
        damage_reps = _nanmean_rows(np.stack([c.damage_reps for c in members]))
        entry: dict[str, Any] = {"n_cells": len(members), "damage": _interval(damage, damage_reps), "ratio": {}}
        for name in SIGNALS:
            xs = [c.excess[name] for c in members]
            if not all(math.isfinite(x) for x in xs):
                entry["ratio"][name] = {"point": None, "lo": None, "hi": None}
                continue
            excess = float(np.mean(xs))
            excess_reps = _nanmean_rows(np.stack([c.excess_reps[name] for c in members]))
            point = excess / damage if damage != 0 else math.nan
            with np.errstate(invalid="ignore", divide="ignore"):
                reps = np.where(damage_reps != 0, excess_reps / damage_reps, np.nan)
            entry["ratio"][name] = _interval(point, reps)
            entry["ratio"][name]["excess_mean"] = excess
        out[family] = entry
    return out


def _trajectory_lengths(forks: Sequence[ForkRecord]) -> dict[tuple[str, str, str, str, str, int], int]:
    lengths: dict[tuple[str, str, str, str, str, int], int] = {}
    for f in forks:
        k = f.key
        ident = (k.protocol, k.stage, k.workload, k.model, k.task_id, k.repetition)
        lengths[ident] = max(lengths.get(ident, 0), k.call_index + 1)
    return lengths


def step_profiles(
    forks: Sequence[ForkRecord],
    *,
    n_relative_bins: int = 10,
    max_absolute_index: int = 20,
    applied_only: bool = True,
) -> dict[str, Any]:
    """Per-cell fork-signal mean by trajectory position, minus the C0 mean at that position.

    Relative position is ``call_index / n_eligible`` where ``n_eligible`` is
    the trajectory's largest forked ``call_index + 1``. Absolute bins are
    ``0 .. max_absolute_index - 1`` plus an overflow bin.
    """
    lengths = _trajectory_lengths(forks)
    sums: dict[tuple[Cell, str, str, int], list[float]] = defaultdict(list)
    for f in forks:
        if applied_only and f.arm != AA_ARM and not f.change_applied:
            continue
        k = f.key
        n = lengths[(k.protocol, k.stage, k.workload, k.model, k.task_id, k.repetition)]
        rel = min(int(n_relative_bins * k.call_index / n), n_relative_bins - 1)
        ab = min(k.call_index, max_absolute_index)
        for name in SIGNALS:
            value = f.signals.get(name)
            if value is None:
                continue
            cell = (k.workload, k.model, f.arm)
            sums[(cell, name, "relative", rel)].append(value)
            sums[(cell, name, "absolute", ab)].append(value)
    out: dict[str, Any] = {}
    cells = sorted({key[0] for key in sums})
    for cell in cells:
        if cell[2] == AA_ARM:
            continue
        aa_cell = (cell[0], cell[1], AA_ARM)
        cell_out: dict[str, Any] = {}
        for name in SIGNALS:
            per_kind: dict[str, Any] = {}
            for kind, n_bins in (("relative", n_relative_bins), ("absolute", max_absolute_index + 1)):
                rows = []
                for b in range(n_bins):
                    values = sums.get((cell, name, kind, b))
                    if not values:
                        continue
                    aa = sums.get((aa_cell, name, kind, b))
                    mean = float(np.mean(values))
                    rows.append(
                        {
                            "bin": b,
                            "n": len(values),
                            "mean": mean,
                            "aa_mean": float(np.mean(aa)) if aa else None,
                            "excess": mean - float(np.mean(aa)) if aa else None,
                        }
                    )
                per_kind[kind] = rows
            cell_out[name] = per_kind
        out[cell_key(cell)] = cell_out
    return out


def first_divergence(a: Sequence[str], b: Sequence[str]) -> int | None:
    """First index at which two action traces differ; ``None`` if identical."""
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return i
    if len(a) != len(b):
        return min(len(a), len(b))
    return None


def _divergence_summary(pairs: Sequence[tuple[Sequence[str], Sequence[str]]]) -> dict[str, Any]:
    firsts: list[int] = []
    relative: list[float] = []
    for a, b in pairs:
        idx = first_divergence(a, b)
        if idx is not None:
            firsts.append(idx)
            relative.append(idx / max(len(a), len(b)))
    return {
        "n_pairs": len(pairs),
        "diverged_rate": _num(len(firsts) / len(pairs)) if pairs else None,
        "first_index_median": _num(float(np.median(firsts))) if firsts else None,
        "first_index_mean": _num(float(np.mean(firsts))) if firsts else None,
        "relative_position_median": _num(float(np.median(relative))) if relative else None,
    }


def trajectory_divergence(trajs: Sequence[TrajectoryRecord]) -> dict[str, Any]:
    """Where changed trajectories first leave the baseline action trace (H4).

    Every changed repetition is compared with every baseline repetition of the
    same task; the baseline-vs-baseline pairs (distinct repetitions) give the
    trajectory-level A/A reference per group.
    """
    by: dict[Cell, dict[str, list[TrajectoryRecord]]] = defaultdict(lambda: defaultdict(list))
    for t in trajs:
        by[(t.workload, t.model, t.arm)][t.task_id].append(t)
    out: dict[str, Any] = {}
    for cell in sorted(by):
        workload, model, arm = cell
        base = by.get((workload, model, BASELINE_ARM), {})
        pairs: list[tuple[Sequence[str], Sequence[str]]] = []
        for task, rows in by[cell].items():
            base_rows = base.get(task, [])
            if arm == BASELINE_ARM:
                for i, x in enumerate(rows):
                    for y in rows[i + 1 :]:
                        pairs.append((x.action_trace, y.action_trace))
            else:
                for x in rows:
                    for y in base_rows:
                        pairs.append((y.action_trace, x.action_trace))
        key = cell_key(cell) if arm != BASELINE_ARM else cell_key((workload, model, "baseline_aa"))
        out[key] = _divergence_summary(pairs)
    return out


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


@dataclass
class Analysis:
    """Result of :func:`analyze`: JSON report plus replicates for reuse (replay)."""

    report: dict[str, Any]
    cells: dict[Cell, CellStats]


def analyze(
    records: Iterable[ForkRecord | TrajectoryRecord],
    *,
    margin: float = DEFAULT_MARGIN,
    n_boot: int = DEFAULT_N_BOOT,
    n_perm: int = 10_000,
    seed: int = DEFAULT_SEED,
    stage: str | None = None,
    families: Mapping[str, str] | None = None,
    applied_only: bool = True,
) -> Analysis:
    """Run H1-H4. See :func:`summarize` for the JSON shape."""
    family_map = dict(DEFAULT_FAMILIES if families is None else families)
    forks, trajs = _split(records, stage)
    groups = _build_groups(forks, trajs, n_boot, seed, applied_only)

    h1 = {f"{g[0]}|{g[1]}": _aa_floor(data) for g, data in groups.items() if AA_ARM in data.forks}

    cells: dict[Cell, CellStats] = {}
    for g, data in groups.items():
        arms = (set(data.traj) | set(data.forks)) - {BASELINE_ARM, AA_ARM}
        for arm in sorted(arms):
            cells[(g[0], g[1], arm)] = _cell_stats(g, data, arm, margin, family_map)

    scopes: dict[str, list[CellStats]] = defaultdict(list)
    for c in cells.values():
        scopes[c.cell[0]].append(c)
        scopes["pooled"].append(c)
    h2 = {scope: _association(members, n_perm, seed, scope) for scope, members in sorted(scopes.items())}
    h3 = {scope: result.pop("h3_auroc_difference") for scope, result in h2.items()}

    report = {
        "config": {
            "margin": margin,
            "n_boot": n_boot,
            "n_perm": n_perm,
            "seed": seed,
            "stage": stage,
            "applied_only": applied_only,
            "n_fork_records": len(forks),
            "n_trajectory_records": len(trajs),
        },
        "h1_aa_floor": h1,
        "cells": {cell_key(k): v.report for k, v in sorted(cells.items())},
        "h2_association": h2,
        "h3_action_vs_text": h3,
        "h4": {
            "family_ratio": _family_ratios(list(cells.values())),
            "step_profiles": step_profiles(forks, applied_only=applied_only),
            "trajectory_first_divergence": trajectory_divergence(trajs),
        },
    }
    return Analysis(report=_jsonable(report), cells=cells)


def summarize(records: Iterable[ForkRecord | TrajectoryRecord], **kwargs: Any) -> dict[str, Any]:
    """JSON-serializable report of H1-H4 (protocol Sections 2 and 7.2).

    Keys: ``config``, ``h1_aa_floor`` (per ``workload|model``), ``cells``
    (per ``workload|model|arm``: damage interval and label, secondary damage,
    per-signal excess), ``h2_association`` and ``h3_action_vs_text`` (per
    workload and ``pooled``), ``h4``. Keyword arguments go to :func:`analyze`.
    """
    return analyze(records, **kwargs).report


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (np.floating, float)):
        return _num(float(value))
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.bool_):
        return bool(value)
    return value


__all__ = [
    "AA_ARM",
    "BASELINE_ARM",
    "DEFAULT_FAMILIES",
    "Analysis",
    "CellStats",
    "analyze",
    "auroc",
    "cell_key",
    "classify",
    "cluster_bootstrap_weights",
    "first_divergence",
    "holm",
    "rankdata",
    "spearman",
    "stable_seed",
    "step_profiles",
    "summarize",
    "trajectory_divergence",
]
