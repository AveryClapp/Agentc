"""Consume one frozen original plan at the existing SDK adapter seam."""

from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterator

from agentc._optimizer import Plan


@dataclass
class _PreparedPlan:
    plan: Plan
    protocol: str
    request_json: str
    call_site_id: str
    dispatch_client: Any = None
    consumed: bool = False


_current: ContextVar[_PreparedPlan | None] = ContextVar(
    "agentc_prepared_plan", default=None
)


def request_json(kwargs: dict[str, Any]) -> str:
    return json.dumps(kwargs, sort_keys=True, separators=(",", ":"), allow_nan=False)


@contextmanager
def prepared_plan(
    plan: Plan,
    protocol: str,
    kwargs: dict[str, Any],
    call_site_id: str,
    *,
    dispatch_client: Any = None,
) -> Iterator[None]:
    token = _current.set(
        _PreparedPlan(
            plan, protocol, request_json(kwargs), call_site_id, dispatch_client
        )
    )
    try:
        yield
    finally:
        _current.reset(token)


def consume_prepared_plan(
    kwargs: dict[str, Any], protocol: str
) -> tuple[Plan, str] | None:
    staged = _current.get()
    if staged is None:
        return None
    payload = kwargs
    if staged.dispatch_client is not None:
        payload = dict(kwargs)
        if payload.pop("client", None) is not staged.dispatch_client:
            raise RuntimeError("prepared provider dispatch client changed")
    if (
        staged.consumed
        or staged.protocol != protocol
        or staged.request_json != request_json(payload)
    ):
        raise RuntimeError(
            "prepared provider request does not match its single-use original plan"
        )
    staged.consumed = True
    from agentc._batch_observation import note_plan

    note_plan(staged.plan)
    return staged.plan, staged.call_site_id
