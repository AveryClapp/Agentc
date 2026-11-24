"""Splits, seeds and fork assignment."""

from __future__ import annotations

from collections import Counter

from bench.change_safety.assignment import (
    fork_arm,
    ordered_id_digest,
    provider_seed,
    split_order,
    splits,
)

IDS = [f"task_{i:03d}" for i in range(1, 98)]


def test_predecessor_digest_convention_is_newline_terminated():
    assert ordered_id_digest(["a", "b"]) == __import__("hashlib").sha256(b"a\nb\n").hexdigest()


def test_splits_are_disjoint_and_sized():
    s = splits("tau2-banking-knowledge", IDS)
    assert (len(s.calibration), len(s.test), len(s.not_selected)) == (30, 67, 0)
    assert not set(s.calibration) & set(s.test)
    assert split_order("tau2-banking-knowledge", IDS) == split_order("tau2-banking-knowledge", IDS[::-1])


def test_namespace_changes_order():
    assert split_order("w", IDS) != split_order("w", IDS, namespace="agentc-mlsys2027-v1")


def test_fork_arm_is_deterministic_with_c0_share():
    arms = ["C1", "C3", "X1", "B1"]
    draws = [
        fork_arm(workload="w", task_id=t, repetition=0, call_index=i, arms=arms)
        for t in IDS for i in range(20)
    ]
    counts = Counter(draws)
    assert set(counts) == {"C0", *arms}
    assert abs(counts["C0"] / len(draws) - 0.25) < 0.03
    assert draws == [
        fork_arm(workload="w", task_id=t, repetition=0, call_index=i, arms=list(reversed(arms)))
        for t in IDS for i in range(20)
    ]
    assert fork_arm(workload="w", task_id="t", repetition=0, call_index=0, arms=[]) == "C0"


def test_provider_seed_range():
    seed = provider_seed("w", "t", "C1", 0)
    assert 0 <= seed < 2**31 - 1 and seed != provider_seed("w", "t", "C1", 1)
