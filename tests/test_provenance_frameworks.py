"""Smoke tests for the framework provenance adapters.

Each adapter (langgraph / crewai / autogen) is supposed to patch its
framework's public surface so that the value flowing out of a node /
task / agent turn is tagged with :class:`LlmOutput(span_id=<current>)`.
If the framework isn't installed, ``install()`` must return ``False``
without raising, and the optimizer falls back to ``Literal``
everywhere.

We don't want to depend on real framework installs for unit tests — so
we register minimal shim modules via ``sys.modules`` that mirror the
attribute shape the adapter targets, install the adapter, exercise the
wrapped method, and assert the returned payload's tag.
"""

from __future__ import annotations

import sys
import types
from contextlib import contextmanager
from typing import Iterator

import pytest

from agentc._context import SpanContext, set_current_span
from agentc._provenance import (
    PROVENANCE_UNSET,
    LlmOutput,
    clear,
    tag_of,
)


@pytest.fixture(autouse=True)
def _reset_tags() -> Iterator[None]:
    clear()
    yield
    clear()


@contextmanager
def _active_span(span_id: str = "abcdef0123456789") -> Iterator[SpanContext]:
    ctx = SpanContext(span_id=span_id, trace_id="t" * 16, name="test.span")
    set_current_span(ctx)
    try:
        yield ctx
    finally:
        set_current_span(None)


# ---------------------------------------------------------------------------
# langgraph
# ---------------------------------------------------------------------------


def _install_fake_langgraph() -> type:
    """Register a minimal ``langgraph.graph`` module with a ``StateGraph``
    class that has an ``add_node(name, fn)`` method the adapter can
    patch. Returns the fake ``StateGraph`` class."""

    class StateGraph:
        def __init__(self) -> None:
            self.nodes: dict[str, object] = {}

        def add_node(self, name: str, action: object) -> None:
            self.nodes[name] = action

    graph_mod = types.ModuleType("langgraph.graph")
    graph_mod.StateGraph = StateGraph  # type: ignore[attr-defined]
    pkg = types.ModuleType("langgraph")
    pkg.graph = graph_mod  # type: ignore[attr-defined]
    sys.modules["langgraph"] = pkg
    sys.modules["langgraph.graph"] = graph_mod
    return StateGraph


def _cleanup_langgraph() -> None:
    from agentc._provenance_frameworks import langgraph as adapter

    adapter.uninstall()
    sys.modules.pop("langgraph.graph", None)
    sys.modules.pop("langgraph", None)


def test_langgraph_missing_framework_returns_false():
    # Ensure the real+fake module is absent.
    sys.modules.pop("langgraph.graph", None)
    sys.modules.pop("langgraph", None)
    from agentc._provenance_frameworks import langgraph as adapter

    # uninstall first so any prior test leakage doesn't short-circuit.
    adapter.uninstall()
    try:
        assert adapter.install() is False
    finally:
        adapter.uninstall()


def test_langgraph_tags_node_output_under_span():
    StateGraph = _install_fake_langgraph()
    try:
        from agentc._provenance_frameworks import langgraph as adapter

        assert adapter.install() is True
        # Idempotent.
        assert adapter.install() is True

        g = StateGraph()

        def my_node(state: dict) -> dict:
            return {"answer": "hello", "count": 1}

        g.add_node("n1", my_node)
        wrapped = g.nodes["n1"]
        assert callable(wrapped)

        with _active_span("1234567812345678"):
            out = wrapped({})  # type: ignore[operator]

        assert out == {"answer": "hello", "count": 1}
        # Per-value tags for dict payloads.
        for v in out.values():
            src = tag_of(v)
            assert isinstance(src, LlmOutput), f"expected LlmOutput, got {src!r}"
            assert src.span_id == "1234567812345678"
    finally:
        _cleanup_langgraph()


def test_langgraph_pass_through_without_span():
    StateGraph = _install_fake_langgraph()
    try:
        from agentc._provenance_frameworks import langgraph as adapter

        assert adapter.install() is True
        g = StateGraph()

        def my_node(state: dict) -> dict:
            return {"answer": "no-span"}

        g.add_node("n1", my_node)
        wrapped = g.nodes["n1"]
        out = wrapped({})  # type: ignore[operator]
        assert out == {"answer": "no-span"}
        # No active span → nothing tagged.
        for v in out.values():
            assert tag_of(v) is PROVENANCE_UNSET
    finally:
        _cleanup_langgraph()


# ---------------------------------------------------------------------------
# crewai
# ---------------------------------------------------------------------------


def _install_fake_crewai() -> type:
    class Task:
        def __init__(self, fn: object) -> None:
            self._fn = fn

        def execute_sync(self, *args: object, **kwargs: object) -> object:
            return self._fn(*args, **kwargs)  # type: ignore[operator]

    task_mod = types.ModuleType("crewai.task")
    task_mod.Task = Task  # type: ignore[attr-defined]
    pkg = types.ModuleType("crewai")
    pkg.task = task_mod  # type: ignore[attr-defined]
    sys.modules["crewai"] = pkg
    sys.modules["crewai.task"] = task_mod
    return Task


def _cleanup_crewai() -> None:
    from agentc._provenance_frameworks import crewai as adapter

    adapter.uninstall()
    sys.modules.pop("crewai.task", None)
    sys.modules.pop("crewai", None)


def test_crewai_missing_framework_returns_false():
    sys.modules.pop("crewai.task", None)
    sys.modules.pop("crewai", None)
    from agentc._provenance_frameworks import crewai as adapter

    adapter.uninstall()
    try:
        assert adapter.install() is False
    finally:
        adapter.uninstall()


def test_crewai_tags_task_output_under_span():
    Task = _install_fake_crewai()
    try:
        from agentc._provenance_frameworks import crewai as adapter

        assert adapter.install() is True

        payload = {"result": "done"}
        t = Task(lambda: payload)

        with _active_span("feedfacefeedface"):
            out = t.execute_sync()

        assert out is payload
        src = tag_of(out)
        assert isinstance(src, LlmOutput)
        assert src.span_id == "feedfacefeedface"
    finally:
        _cleanup_crewai()


def test_crewai_pass_through_without_span():
    Task = _install_fake_crewai()
    try:
        from agentc._provenance_frameworks import crewai as adapter

        assert adapter.install() is True

        payload = {"result": "untagged"}
        t = Task(lambda: payload)
        out = t.execute_sync()
        assert out is payload
        assert tag_of(out) is PROVENANCE_UNSET
    finally:
        _cleanup_crewai()


# ---------------------------------------------------------------------------
# autogen
# ---------------------------------------------------------------------------


def _install_fake_autogen() -> type:
    class ConversableAgent:
        def __init__(self, reply: object) -> None:
            self._reply = reply

        def generate_reply(self, *args: object, **kwargs: object) -> object:
            return self._reply

    pkg = types.ModuleType("autogen")
    pkg.ConversableAgent = ConversableAgent  # type: ignore[attr-defined]
    sys.modules["autogen"] = pkg
    return ConversableAgent


def _cleanup_autogen() -> None:
    from agentc._provenance_frameworks import autogen as adapter

    adapter.uninstall()
    sys.modules.pop("autogen", None)
    sys.modules.pop("pyautogen", None)


def test_autogen_missing_framework_returns_false():
    sys.modules.pop("autogen", None)
    sys.modules.pop("pyautogen", None)
    from agentc._provenance_frameworks import autogen as adapter

    adapter.uninstall()
    try:
        assert adapter.install() is False
    finally:
        adapter.uninstall()


def test_autogen_tags_reply_under_span():
    ConversableAgent = _install_fake_autogen()
    try:
        from agentc._provenance_frameworks import autogen as adapter

        assert adapter.install() is True

        reply = {"content": "hi from agent"}
        agent = ConversableAgent(reply)

        with _active_span("cafebabecafebabe"):
            out = agent.generate_reply()

        assert out is reply
        src = tag_of(out)
        assert isinstance(src, LlmOutput)
        assert src.span_id == "cafebabecafebabe"
    finally:
        _cleanup_autogen()


def test_autogen_pass_through_without_span():
    ConversableAgent = _install_fake_autogen()
    try:
        from agentc._provenance_frameworks import autogen as adapter

        assert adapter.install() is True

        reply = {"content": "untagged"}
        agent = ConversableAgent(reply)
        out = agent.generate_reply()
        assert out is reply
        assert tag_of(out) is PROVENANCE_UNSET
    finally:
        _cleanup_autogen()


# ---------------------------------------------------------------------------
# install_all
# ---------------------------------------------------------------------------


def test_install_all_reports_per_framework_status():
    sys.modules.pop("langgraph", None)
    sys.modules.pop("langgraph.graph", None)
    sys.modules.pop("crewai", None)
    sys.modules.pop("crewai.task", None)
    sys.modules.pop("autogen", None)
    sys.modules.pop("pyautogen", None)

    from agentc._provenance_frameworks import install_all
    from agentc._provenance_frameworks import (
        autogen as a,
        crewai as c,
        langgraph as lg,
    )

    # Start clean.
    lg.uninstall()
    c.uninstall()
    a.uninstall()

    try:
        _install_fake_langgraph()
        _install_fake_crewai()
        _install_fake_autogen()

        status = install_all()
        assert status == {"langgraph": True, "crewai": True, "autogen": True}
    finally:
        _cleanup_langgraph()
        _cleanup_crewai()
        _cleanup_autogen()
