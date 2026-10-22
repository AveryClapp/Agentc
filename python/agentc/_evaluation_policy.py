"""Immutable request-policy declaration for controlled benchmark execution.

This seam executes a previously frozen configuration. It does not select a
policy or establish that its configuration came from admissible calibration.
Benchmark acquisition must validate that evidence and stage before dispatch.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Iterator

VERSION = "frozen-request-policy-v1"
MODES = frozenset({"best_static_joint", "route_then_rewrite", "rewrite_then_route"})


@dataclass(frozen=True)
class FrozenEvaluationPolicy:
    mode: str
    calibration_lock_sha256: str
    configuration_json: str

    def __post_init__(self) -> None:
        if (
            self.mode not in MODES
            or len(self.calibration_lock_sha256) != 64
            or any(c not in "0123456789abcdef" for c in self.calibration_lock_sha256)
            or not isinstance(json.loads(self.configuration_json), dict)
        ):
            raise ValueError("invalid frozen evaluation declaration")

    @classmethod
    def from_configuration(
        cls,
        *,
        mode: str,
        calibration_lock_sha256: str,
        configuration: dict[str, Any],
    ) -> FrozenEvaluationPolicy:
        return cls(
            mode,
            calibration_lock_sha256,
            json.dumps(
                configuration,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ),
        )

    def declaration(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "policy_version": VERSION,
            "mode": self.mode,
            "calibration_lock_sha256": self.calibration_lock_sha256,
            "configuration_sha256": sha256(
                self.configuration_json.encode()
            ).hexdigest(),
            "configuration_json": self.configuration_json,
        }


_current: ContextVar[FrozenEvaluationPolicy | None] = ContextVar(
    "agentc_frozen_evaluation_policy",
    default=None,
)


def current_evaluation_policy() -> FrozenEvaluationPolicy | None:
    return _current.get()


@contextmanager
def evaluation_policy_scope(policy: FrozenEvaluationPolicy) -> Iterator[None]:
    if type(policy) is not FrozenEvaluationPolicy:
        raise ValueError("evaluation scope requires an immutable policy")
    token = _current.set(policy)
    try:
        yield
    finally:
        _current.reset(token)
