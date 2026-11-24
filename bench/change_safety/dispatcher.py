"""Workload-agnostic routing for study trajectories.

A workload driver (tau2, SWE-agent) owns its agent loop and grading. It sends
every evaluated-agent provider call to :meth:`StudyDispatcher.agent_call` and
every other actor's call to :meth:`StudyDispatcher.passthrough`. The
dispatcher

- applies the trajectory's change to every eligible call in a rollout,
- sends one fork per eligible call in a baseline run, assigned by
  :func:`assignment.fork_arm` and dispatched outside Agentc's interception,
- bills every call through the spend ledger, and
- accumulates the per-trajectory counts for :class:`TrajectoryRecord`.
"""

from __future__ import annotations

import os
import sys
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Callable, Iterator, Mapping

from agentc.change_safety.normalize import action_signature
from agentc.change_safety.records import (
    CallKey,
    ChangeSpec,
    ForkRecord,
    NormalizedResponse,
    TrajectoryRecord,
    to_line,
)
from agentc.change_safety.shadow import ShadowRunner, timed_dispatch
from agentc.change_safety.signals import TerminationMarkers

from bench.change_safety.assignment import PROTOCOL, fork_arm
from bench.change_safety.spend import SpendLedger

Dispatch = Callable[[Mapping[str, Any]], Any]


class ProviderCallFailed(RuntimeError):
    """An evaluated-agent call failed at the provider; the workload sees it."""


_call_site: ContextVar[str | None] = ContextVar("change_safety_call_site", default=None)


def mark_call_site() -> bool:
    """Record the caller's frame as Agentc will name it; always returns True.

    Call it on the same source line as the provider call, e.g.
    ``return mark_call_site() and litellm.completion(**request)``, so the
    recorded ``module:function:line`` equals Agentc's derived call-site ID.
    C5 and C6 need that ID to read the live call-site profile.
    """
    frame = sys._getframe(1)
    _call_site.set(f"{frame.f_globals.get('__name__', '')}:{frame.f_code.co_name}:{frame.f_lineno}")
    return True


#: Agentc settings for a study. The master switch makes every plan a
#: pass-through, so reference calls are never rewritten, while eligible calls
#: are still planned and observed: that builds the call-site profile C5 and C6
#: read. Exploration and shadow sampling are off because their provider calls
#: would bypass the spend ledger.
STUDY_ENVIRONMENT = {
    "AGENTC_OPTIMIZE": "false",
    "AGENTC_OPTIMIZE_EXPLORATION": "false",
    "AGENTC_OPTIMIZE_SHADOW": "0",
}

_OFF = {"0", "false", "no", "off"}


def configure_study_environment() -> None:
    """Disable Agentc's own counterfactual calls; call before ``agentc.init``."""
    os.environ.update(STUDY_ENVIRONMENT)


def require_study_environment() -> None:
    optimize = os.environ.get("AGENTC_OPTIMIZE", "true").strip().lower()
    exploration = os.environ.get("AGENTC_OPTIMIZE_EXPLORATION", "true").strip().lower()
    shadow = os.environ.get("AGENTC_OPTIMIZE_SHADOW", "0.02").strip()
    if optimize not in _OFF or exploration not in _OFF or float(shadow) != 0.0:
        raise RuntimeError(
            "Agentc's optimizer, exploration and shadow sampling must be off during "
            "a study: rewrites would change reference calls, and extra provider "
            "calls would bypass the spend ledger"
        )


@dataclass
class TrajectoryState:
    task_id: str
    repetition: int
    arm: str
    spec: ChangeSpec | None
    fork_arms: Mapping[str, ChangeSpec]
    calls: int = 0
    failed_calls: int = 0
    tool_calls: int = 0
    changed: int = 0
    cost: float = 0.0
    tokens: dict[str, int | None] = field(
        default_factory=lambda: {"input": 0, "output": 0, "cached": 0, "creation": 0}
    )
    trace: list[str] = field(default_factory=list)

    def account(self, response: NormalizedResponse) -> None:
        self.calls += 1
        self.tool_calls += len(response.tool_calls)
        self.trace.append(action_signature(response))
        self.cost += response.cost_usd or 0.0
        for name, value in (
            ("input", response.input_tokens),
            ("output", response.output_tokens),
            ("cached", response.cached_input_tokens),
            ("creation", response.cache_creation_input_tokens),
        ):
            current = self.tokens[name]
            self.tokens[name] = None if current is None or value is None else current + value


class StudyDispatcher:
    def __init__(
        self,
        *,
        stage: str,
        workload: str,
        model: str,
        records: Path,
        ledger: SpendLedger,
        reference: Dispatch,
        fork: Dispatch,
        agent_scope: str,
        other_scope: str,
        markers: TerminationMarkers = TerminationMarkers(),
        embed: bool = True,
        observe_agent: bool = True,
    ):
        require_study_environment()
        #: Mark reference agent calls eligible so Agentc records the call-site
        #: profile. Safe only because the study environment (checked above)
        #: forces every plan to pass through.
        self._observe_agent = observe_agent
        self.stage, self.workload, self.model = stage, workload, model
        self.agent_scope, self.other_scope = agent_scope, other_scope
        self.ledger = ledger
        self._reference, self._fork = reference, fork
        self._markers, self._embed = markers, embed
        self._lock = threading.Lock()
        self._active: TrajectoryState | None = None
        #: Agentc's call-site ID for evaluated-agent calls, once observed.
        self.call_site: str | None = None
        records.parent.mkdir(parents=True, exist_ok=True)
        self._records: IO[str] = records.open("a", encoding="utf-8")

    def close(self) -> None:
        self._records.close()

    def write(self, record: ForkRecord | TrajectoryRecord) -> None:
        with self._lock:
            self._records.write(to_line(record) + "\n")
            self._records.flush()

    def _billed(self, role: str, dispatch: Dispatch) -> Dispatch:
        from agentc._optimization_scope import optimization_scope

        # Innermost scope wins: whatever adapters wrap the workload, Agentc
        # observes study calls but never rewrites them.
        scope = self.agent_scope if role in {"agent", "fork"} else self.other_scope
        eligible = self._observe_agent and role == "agent"

        def call(request: Mapping[str, Any]) -> Any:
            with optimization_scope(scope, optimize=eligible):
                return self.ledger.call(role=role, request=request, dispatch=dispatch)

        return call

    @property
    def active(self) -> TrajectoryState:
        if self._active is None:
            raise RuntimeError("provider call outside an active study trajectory")
        return self._active

    @contextmanager
    def trajectory(
        self, *, task_id: str, repetition: int, arm: str, catalog: Mapping[str, ChangeSpec]
    ) -> Iterator[TrajectoryState]:
        """Scope one trajectory. ``arm`` is ``"baseline"`` or a change ID."""
        if arm != "baseline" and arm not in catalog:
            raise KeyError(arm)
        state = TrajectoryState(
            task_id=task_id,
            repetition=repetition,
            arm=arm,
            spec=None if arm == "baseline" else catalog[arm],
            fork_arms=dict(catalog) if arm == "baseline" else {},
        )
        with self._lock:
            if self._active is not None:
                raise RuntimeError("one trajectory at a time")
            self._active = state
        try:
            yield state
        finally:
            with self._lock:
                self._active = None

    def passthrough(self, role: str, request: Mapping[str, Any]) -> Any:
        """A call by an actor that is never changed (user simulator, judge)."""
        self.active  # noqa: B018 - asserts an active trajectory
        return self._billed(role, self._reference)(request)

    def agent_call(self, request: Mapping[str, Any]) -> Any:
        state = self.active
        index = state.calls + state.failed_calls
        live: Mapping[str, Any] = request
        if state.spec is not None:
            from agentc.change_safety.changes import apply_change

            applied = apply_change(state.spec, request, call_site_id=self.call_site)
            if applied.applied:
                live = applied.request
                state.changed += 1
        delivered, raw = timed_dispatch(self._billed("agent", self._reference), live)
        site = _call_site.get()
        if site is not None:
            self.call_site = site
        if raw is None:
            state.failed_calls += 1
            raise ProviderCallFailed(delivered.error or "provider error")
        state.account(delivered)
        if state.spec is None and state.fork_arms:
            arm = fork_arm(
                workload=self.workload,
                task_id=state.task_id,
                repetition=state.repetition,
                call_index=index,
                arms=[a for a in state.fork_arms if a != "C0"],
            )
            if arm not in state.fork_arms:
                raise KeyError(f"fork arm {arm} has no change spec")
            ShadowRunner(
                dispatch=self._billed("fork", self._fork),
                sink=self.write,
                markers=self._markers,
                embed=self._embed,
            ).fork(
                key=CallKey(
                    protocol=PROTOCOL,
                    stage=self.stage,
                    workload=self.workload,
                    model=self.model,
                    task_id=state.task_id,
                    repetition=state.repetition,
                    call_index=index,
                ),
                spec=state.fork_arms[arm],
                request=request,
                reference=delivered,
                reference_request_tokens=delivered.input_tokens,
                call_site_id=self.call_site,
            )
        return raw

    def finish(
        self, state: TrajectoryState, *, reward: float, termination: str
    ) -> TrajectoryRecord:
        record = TrajectoryRecord(
            protocol=PROTOCOL,
            stage=self.stage,
            workload=self.workload,
            model=self.model,
            task_id=state.task_id,
            repetition=state.repetition,
            arm=state.arm,
            change_identity=None if state.spec is None else state.spec.identity,
            reward=float(min(1.0, max(0.0, reward))),
            model_calls=state.calls,
            tool_calls=state.tool_calls,
            eligible_calls=state.calls,
            changed_calls=state.changed,
            termination=termination,
            cost_usd=state.cost,
            input_tokens=state.tokens["input"],
            output_tokens=state.tokens["output"],
            cached_input_tokens=state.tokens["cached"],
            cache_creation_input_tokens=state.tokens["creation"],
            action_trace=tuple(state.trace),
        )
        self.write(record)
        return record


__all__ = [
    "Dispatch",
    "ProviderCallFailed",
    "STUDY_ENVIRONMENT",
    "StudyDispatcher",
    "TrajectoryState",
    "configure_study_environment",
    "mark_call_site",
    "require_study_environment",
]
