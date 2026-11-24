"""Synthetic change-safety records built with the records.py dataclasses."""

from __future__ import annotations

import random
from typing import Mapping

from agentc.change_safety.records import (
    CallKey,
    ForkRecord,
    NormalizedResponse,
    SignalValues,
    TrajectoryRecord,
)

#: arm -> (S4 disagreement probability, S1 low, S1 high, reward drop)
ArmSpec = tuple[float, float, float, float]


def make_fork(workload: str, model: str, task: str, rep: int, call: int, arm: str, s1: float, s4: float, cost: float = 0.001) -> ForkRecord:
    response = NormalizedResponse(kind="text", text="ok", cost_usd=cost)
    return ForkRecord(
        key=CallKey("agentc-change-safety-v1", "T", workload, model, task, rep, call),
        arm=arm,
        change_identity=f"id-{arm}",
        change_applied=True,
        change_evidence={},
        baseline=response,
        fork=response,
        signals=SignalValues(S1=s1, S2=s1, S3=s1, S4=s4, S5=0.0),
    )


def make_trajectory(workload: str, model: str, task: str, rep: int, arm: str, reward: float, calls: int, trace: tuple[str, ...] = ()) -> TrajectoryRecord:
    return TrajectoryRecord(
        protocol="agentc-change-safety-v1",
        stage="T",
        workload=workload,
        model=model,
        task_id=task,
        repetition=rep,
        arm=arm,
        change_identity=None if arm == "baseline" else f"id-{arm}",
        reward=reward,
        model_calls=calls,
        tool_calls=calls // 2,
        eligible_calls=calls,
        changed_calls=0 if arm == "baseline" else calls,
        termination="done",
        cost_usd=0.01,
        input_tokens=1000,
        output_tokens=100,
        cached_input_tokens=0,
        cache_creation_input_tokens=0,
        action_trace=trace,
    )


def build_records(
    arms: Mapping[str, ArmSpec],
    *,
    workloads: tuple[str, ...] = ("tau2",),
    n_tasks: int = 20,
    reps: int = 2,
    calls: int = 10,
    seed: int = 0,
) -> list[ForkRecord | TrajectoryRecord]:
    """Forks for C0 and every arm, plus baseline and changed trajectories."""
    rng = random.Random(seed)
    out: list[ForkRecord | TrajectoryRecord] = []
    all_arms = {"C0": (0.1, 0.0, 0.04, 0.0), **arms}
    for workload in workloads:
        for t in range(n_tasks):
            task = f"task-{t:03d}"
            base_reward = 1.0 if t % 4 else 0.0
            for rep in range(reps):
                out.append(make_trajectory(workload, "m", task, rep, "baseline", base_reward, calls, tuple("abcdefghij"[:calls])))
                for arm, (p, lo, hi, drop) in all_arms.items():
                    for call in range(calls):
                        s4 = 1.0 if rng.random() < p else 0.0
                        out.append(make_fork(workload, "m", task, rep, call, arm, rng.uniform(lo, hi), s4))
                    if arm == "C0":
                        continue
                    reward = base_reward
                    if drop > 0 and base_reward > 0 and rng.random() < drop:
                        reward = 0.0
                    trace = tuple("abcdefghij"[:calls]) if drop == 0 else tuple("abcdeXYZWV"[:calls])
                    out.append(make_trajectory(workload, "m", task, rep, arm, reward, calls + (2 if drop else 0), trace))
    return out


import pytest  # noqa: E402

from bench.change_safety.dispatcher import STUDY_ENVIRONMENT  # noqa: E402


@pytest.fixture
def study_env(monkeypatch):
    """The study's Agentc settings, undone after the test."""
    for key, value in STUDY_ENVIRONMENT.items():
        monkeypatch.setenv(key, value)
