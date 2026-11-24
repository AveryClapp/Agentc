"""Record schema shared by the change-safety runtime and its offline analysis.

Every record is a frozen dataclass that serializes to one canonical JSON line
(sorted keys, no NaN). The study harness writes these records and the analysis
reads them back; nothing else crosses that boundary.

Direction convention: every signal is a *divergence*, so larger means the
changed call behaved more differently from the reference call. ``None`` means
the signal could not be computed and must never be read as zero.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

SCHEMA_VERSION = 1

#: Change families from protocol Section 4.
FAMILIES = frozenset(
    {
        "aa_control",
        "model_swap",
        "model_migration",
        "context_removal",
        "output_cap",
        "seeded_harm",
        "benign",
    }
)

#: Signal identifiers from protocol Section 6.1.
SIGNALS = ("S1", "S2", "S3", "S4", "S5")


def canonical_json(value: Any) -> str:
    """Deterministic JSON used for hashing and for record lines."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha256_hex(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _check_unit(name: str, value: float | None) -> None:
    if value is not None and (not math.isfinite(value) or not 0.0 <= value <= 1.0):
        raise ValueError(f"{name} must be in [0, 1] or None, got {value!r}")


@dataclass(frozen=True)
class ChangeSpec:
    """A deterministic change to an eligible call, fixed before a stage runs."""

    change_id: str
    family: str
    params: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.change_id or self.family not in FAMILIES:
            raise ValueError(f"invalid change spec {self.change_id!r}/{self.family!r}")
        # Freeze a canonical copy so the identity cannot drift after creation.
        object.__setattr__(self, "params", json.loads(canonical_json(dict(self.params))))

    @property
    def identity(self) -> str:
        """Content hash binding the change to its exact parameters."""
        return sha256_hex(
            {"change_id": self.change_id, "family": self.family, "params": self.params}
        )


@dataclass(frozen=True)
class AppliedChange:
    """Result of applying a change to one request.

    ``applied`` is False when the change is ineligible for this request (for
    example provider compaction on a model that lacks it) or when it fires
    but leaves the request unchanged. Ineligible forks are recorded, never
    silently replaced by another change.
    """

    request: Mapping[str, Any]
    applied: bool
    ineligible_reason: str | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolAction:
    name: str
    arguments_json: str  # canonical JSON; volatile call IDs are excluded


@dataclass(frozen=True)
class NormalizedResponse:
    """Provider-neutral view of one model response.

    ``kind`` is ``"text"`` for a plain assistant message, ``"tool_calls"``
    when at least one tool call is present, and ``"error"`` when the call
    failed before a response existed.
    """

    kind: str
    text: str = ""
    tool_calls: tuple[ToolAction, ...] = ()
    finish_reason: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cost_usd: float | None = None
    latency_s: float | None = None
    #: Provider-reported context edits (``context_management.applied_edits``).
    applied_edits: tuple[Mapping[str, Any], ...] | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"text", "tool_calls", "error"}:
            raise ValueError(f"invalid response kind {self.kind!r}")
        if (self.kind == "tool_calls") != bool(self.tool_calls):
            raise ValueError("tool_calls kind requires at least one tool call")


@dataclass(frozen=True)
class SignalValues:
    S1: float | None = None  # lexical Jaccard divergence
    S2: float | None = None  # normalized Jaccard divergence
    S3: float | None = None  # embedding cosine distance
    S4: float | None = None  # action disagreement
    S5: float | None = None  # termination and length divergence
    #: Components behind S4 and S5, for diagnostics only.
    components: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in SIGNALS:
            _check_unit(name, getattr(self, name))

    def get(self, name: str) -> float | None:
        if name not in SIGNALS:
            raise KeyError(name)
        value: float | None = getattr(self, name)
        return value


@dataclass(frozen=True)
class CallKey:
    """Identity of one eligible call inside one baseline trajectory."""

    protocol: str
    stage: str
    workload: str
    model: str
    task_id: str
    repetition: int
    call_index: int

    @property
    def trajectory_id(self) -> str:
        return sha256_hex(
            [self.protocol, self.stage, self.workload, self.model, self.task_id, self.repetition]
        )


@dataclass(frozen=True)
class ForkRecord:
    """One call-level fork (protocol Section 5.1).

    The baseline response is what the agent saw. The fork response was never
    returned to the agent.
    """

    key: CallKey
    arm: str  # change_id of the fork; "C0" is the A/A control
    change_identity: str
    change_applied: bool
    change_evidence: Mapping[str, Any]
    baseline: NormalizedResponse
    fork: NormalizedResponse | None
    signals: SignalValues
    baseline_request_tokens: int | None = None
    ineligible_reason: str | None = None
    schema_version: int = SCHEMA_VERSION
    record_type: str = "fork"


@dataclass(frozen=True)
class TrajectoryRecord:
    """One complete trajectory (protocol Section 5.2).

    ``arm`` is ``"baseline"`` for an unchanged rollout, otherwise the
    change_id applied to every eligible call.
    """

    protocol: str
    stage: str
    workload: str
    model: str
    task_id: str
    repetition: int
    arm: str
    change_identity: str | None
    reward: float  # official task reward in [0, 1]
    model_calls: int
    tool_calls: int
    eligible_calls: int
    changed_calls: int
    termination: str
    cost_usd: float | None
    input_tokens: int | None
    output_tokens: int | None
    cached_input_tokens: int | None
    cache_creation_input_tokens: int | None
    #: Ordered action signatures of eligible calls, for per-step profiles (H4).
    action_trace: tuple[str, ...] = ()
    schema_version: int = SCHEMA_VERSION
    record_type: str = "trajectory"

    def __post_init__(self) -> None:
        _check_unit("reward", self.reward)


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def to_line(record: ForkRecord | TrajectoryRecord) -> str:
    return canonical_json(asdict(record))


def _response(data: Mapping[str, Any] | None) -> NormalizedResponse | None:
    if data is None:
        return None
    values = dict(data)
    values["tool_calls"] = tuple(ToolAction(**t) for t in values.get("tool_calls", ()))
    if values.get("applied_edits") is not None:
        values["applied_edits"] = tuple(values["applied_edits"])
    return NormalizedResponse(**values)


def from_line(line: str) -> ForkRecord | TrajectoryRecord:
    data = json.loads(line)
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"unsupported schema version {data.get('schema_version')!r}")
    kind = data.get("record_type")
    if kind == "fork":
        data["key"] = CallKey(**data["key"])
        data["baseline"] = _response(data["baseline"])
        data["fork"] = _response(data["fork"])
        data["signals"] = SignalValues(**data["signals"])
        return ForkRecord(**data)
    if kind == "trajectory":
        data["action_trace"] = tuple(data.get("action_trace", ()))
        return TrajectoryRecord(**data)
    raise ValueError(f"unknown record type {kind!r}")


def read_records(path: str) -> list[ForkRecord | TrajectoryRecord]:
    with open(path, encoding="utf-8") as stream:
        return [from_line(line) for line in stream if line.strip()]


__all__ = [
    "SCHEMA_VERSION",
    "FAMILIES",
    "SIGNALS",
    "AppliedChange",
    "CallKey",
    "ChangeSpec",
    "ForkRecord",
    "NormalizedResponse",
    "SignalValues",
    "ToolAction",
    "TrajectoryRecord",
    "canonical_json",
    "from_line",
    "read_records",
    "sha256_hex",
    "to_line",
]
