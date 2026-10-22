"""Attribution for an actual provider invocation, including counterfactual calls.

The workload owns actor/task identity; SDK adapters own whether an invocation
is primary, exploration, shadow or fallback. Tokens are copied opaquely from
the actual plans, never inferred from request content or decoded here.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterator, Literal

DispatchKind = Literal["primary", "exploration", "shadow", "fallback", "validation"]


@dataclass(frozen=True, slots=True)
class ProviderDispatch:
    kind: DispatchKind
    # The native plan exists before dispatch; observation handles are issued
    # only after a result is observed. Preserve the actual serialized plan
    # without interpreting it or inventing an early observation handle.
    native_plan_json: str | None = None
    parent_native_plan_json: str | None = None
    observation_token: str | None = None
    parent_observation_token: str | None = None
    exploration_lease_token: str | None = None


_current: ContextVar[ProviderDispatch | None] = ContextVar(
    "agentc_provider_dispatch", default=None
)
_owner: ContextVar[Any] = ContextVar("agentc_provider_dispatch_owner", default=None)


def current_provider_dispatch() -> ProviderDispatch | None:
    return _current.get()


def current_provider_plan() -> Any:
    """Private logical owner, retained across validation and copied probe contexts."""
    return _owner.get()


@contextmanager
def sdk_dispatch(plan: Any, *, mutated: bool = False) -> Iterator[None]:
    """Attribute the exact SDK invocation while retaining an auxiliary owner."""
    inherited = _current.get()
    if inherited is not None and inherited.kind in {
        "validation",
        "exploration",
        "shadow",
    }:
        yield
        return
    kind: DispatchKind = (
        "fallback"
        if not mutated and getattr(plan, "dispatch_fallback", False)
        else "primary"
    )
    with provider_dispatch(kind, plan=plan, primary_plan=plan):
        yield


def _token(plan: Any, name: str) -> str | None:
    value = getattr(plan, name, None)
    return value if isinstance(value, str) and value else None


@contextmanager
def provider_dispatch(
    kind: DispatchKind, *, plan: Any = None, primary_plan: Any = None,
    dispatch_plan: Any = None,
) -> Iterator[None]:
    context = ProviderDispatch(
        kind=kind,
        native_plan_json=_token(dispatch_plan, "raw_json")
        if dispatch_plan is not None
        else _token(plan, "raw_json")
        if kind in {"primary", "exploration"}
        else None,
        parent_native_plan_json=_token(primary_plan, "raw_json")
        if kind != "primary"
        else None,
        observation_token=_token(plan, "observation_token")
        if kind in {"primary", "exploration"}
        else None,
        parent_observation_token=_token(primary_plan, "observation_token")
        if kind != "primary"
        else None,
        exploration_lease_token=_token(primary_plan, "exploration_lease_token")
        if kind == "exploration"
        else None,
    )
    token = _current.set(context)
    owner = primary_plan if primary_plan is not None else plan
    owner_token = _owner.set(owner if owner is not None else _owner.get())
    try:
        yield
    finally:
        _owner.reset(owner_token)
        _current.reset(token)
