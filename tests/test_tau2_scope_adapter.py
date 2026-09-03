"""Conformance tests for the tau2 actor-scope adapter."""

from __future__ import annotations

from types import ModuleType
from typing import Any

import pytest

from agentc import optimization_scope_report
from agentc._optimization_scope import (
    _reset_optimization_scope_report,
    decide_optimization,
)
from agentc._scope_adapters import tau2


@pytest.fixture(autouse=True)
def _clean_adapter() -> None:
    tau2.uninstall()
    _reset_optimization_scope_report()
    yield
    tau2.uninstall()


def test_agent_and_user_aliases_receive_distinct_scopes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def generate(*args: Any, **kwargs: Any) -> object:
        calls.append((args, kwargs))
        decide_optimization()
        return kwargs["sentinel"]

    agent_module = ModuleType("tau2.agent.llm_agent")
    user_module = ModuleType("tau2.user.user_simulator")
    agent_module.generate = generate  # type: ignore[attr-defined]
    user_module.generate = generate  # type: ignore[attr-defined]
    modules = {
        agent_module.__name__: agent_module,
        user_module.__name__: user_module,
    }
    monkeypatch.setattr(tau2.importlib, "import_module", modules.__getitem__)

    assert tau2.install()
    assert tau2.install(), "installation must be idempotent"

    agent_marker = object()
    user_marker = object()
    messages = [object()]
    assert agent_module.generate(messages, sentinel=agent_marker) is agent_marker  # type: ignore[attr-defined]
    assert user_module.generate(messages, sentinel=user_marker) is user_marker  # type: ignore[attr-defined]

    assert calls[0][0][0] is messages
    assert calls[1][0][0] is messages
    report = optimization_scope_report()
    assert report["eligible_calls"] == 1
    assert report["excluded_calls"] == 1
    assert report["scopes"] == [
        {
            "name": "tau2.evaluated_assistant",
            "scope_enabled": True,
            "total_calls": 1,
            "eligible_calls": 1,
            "excluded_calls": 0,
            "decision_reasons": {"scope_eligible": 1},
        },
        {
            "name": "tau2.user_simulator",
            "scope_enabled": False,
            "total_calls": 1,
            "eligible_calls": 0,
            "excluded_calls": 1,
            "decision_reasons": {"scope_excluded": 1},
        },
    ]

    tau2.uninstall()
    assert agent_module.generate is generate  # type: ignore[attr-defined]
    assert user_module.generate is generate  # type: ignore[attr-defined]


def test_install_is_atomic_when_one_tau2_actor_seam_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_module = ModuleType("tau2.agent.llm_agent")

    def original() -> None:
        return None

    agent_module.generate = original  # type: ignore[attr-defined]

    def import_module(name: str) -> ModuleType:
        if name == agent_module.__name__:
            return agent_module
        raise ImportError(name)

    monkeypatch.setattr(tau2.importlib, "import_module", import_module)

    assert not tau2.install()
    assert agent_module.generate is original  # type: ignore[attr-defined]
