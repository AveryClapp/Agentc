"""Deterministic splits, seeds, arm order and fork assignment.

Splits and seeds follow Section 4 of the predecessor protocol
(``bench/repro/mlsys-2027-evaluation-protocol.md``) with the namespace
``agentc-change-safety-v1`` in place of ``agentc-mlsys2027-v1``, as required by
Section 3.1 of the change-safety protocol. Fork arms follow Section 5.1: one
fork per eligible call, chosen by a hash of
``(protocol, workload, task, repetition, call index)``, with a fixed share for
the A/A control C0.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Sequence

PROTOCOL = "agentc-change-safety-v1"

#: (calibration, test) sizes per workload; the remainder is not selected.
SPLIT_SIZES = {
    "tau2-banking-knowledge": (30, 67),
    "swebench-verified": (20, 40),
}

C0_SHARE = 0.25


def _digest(*parts: str) -> bytes:
    return hashlib.sha256(b"\x00".join(part.encode() for part in parts)).digest()


@dataclass(frozen=True)
class Splits:
    calibration: tuple[str, ...]
    test: tuple[str, ...]
    not_selected: tuple[str, ...]

    def of(self, stage: str) -> tuple[str, ...]:
        return {"calibration": self.calibration, "test": self.test}[stage]


def split_order(workload: str, task_ids: Sequence[str], namespace: str = PROTOCOL) -> list[str]:
    """Task IDs sorted by split key, ties broken by task ID."""
    if len(set(task_ids)) != len(task_ids):
        raise ValueError("task IDs must be unique")
    return sorted(task_ids, key=lambda task: (_digest(namespace, workload, task), task))


def splits(workload: str, task_ids: Sequence[str]) -> Splits:
    calibration, test = SPLIT_SIZES[workload]
    ordered = split_order(workload, task_ids)
    if len(ordered) < calibration + test:
        raise ValueError(f"{workload} has {len(ordered)} tasks, needs {calibration + test}")
    return Splits(
        calibration=tuple(ordered[:calibration]),
        test=tuple(ordered[calibration : calibration + test]),
        not_selected=tuple(ordered[calibration + test :]),
    )


def ordered_id_digest(task_ids: Sequence[str]) -> str:
    """SHA-256 of the newline-terminated ordered ID list (predecessor Section 4)."""
    return hashlib.sha256("".join(f"{task}\n" for task in task_ids).encode()).hexdigest()


def provider_seed(workload: str, task_id: str, arm: str, repetition: int) -> int:
    """Numeric provider seed, predecessor Section 4."""
    digest = hashlib.sha256(
        b"agentc-run-v1\x00"
        + b"\x00".join(p.encode() for p in (workload, task_id, arm, str(repetition)))
    ).digest()
    return int.from_bytes(digest[:8], "big") % (2**31 - 1)


def arm_order(workload: str, task_id: str, repetition: int, arms: Sequence[str]) -> list[str]:
    """Interleaved arm order for one task and repetition (predecessor Section 4)."""
    return sorted(
        arms,
        key=lambda arm: _digest("agentc-arm-order-v1", workload, task_id, arm, str(repetition)),
    )


def fork_arm(
    *,
    workload: str,
    task_id: str,
    repetition: int,
    call_index: int,
    arms: Sequence[str],
    c0_share: float = C0_SHARE,
    protocol: str = PROTOCOL,
) -> str:
    """The single change forked at one eligible call.

    ``arms`` are the non-control changes eligible for this model, fixed before
    the stage runs. C0 receives ``c0_share`` of calls; the rest are spread
    uniformly over ``arms`` in sorted order.
    """
    if "C0" in arms or not 0.0 < c0_share < 1.0:
        raise ValueError("arms exclude C0 and c0_share must be in (0, 1)")
    draw = int.from_bytes(
        _digest(protocol, workload, task_id, str(repetition), str(call_index))[:8], "big"
    ) / 2**64
    if draw < c0_share or not arms:
        return "C0"
    ordered = sorted(arms)
    index = int((draw - c0_share) / (1.0 - c0_share) * len(ordered))
    return ordered[min(index, len(ordered) - 1)]


__all__ = [
    "C0_SHARE",
    "PROTOCOL",
    "SPLIT_SIZES",
    "Splits",
    "arm_order",
    "fork_arm",
    "ordered_id_digest",
    "provider_seed",
    "split_order",
    "splits",
]
