"""No-network tests for the optional LiteLLM interception adapter."""

from __future__ import annotations

import json
import sys
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentc import optimization_scope, optimization_scope_report
from agentc._lifecycle import _initialized, _shutdown_in_progress
from agentc._optimization_scope import _reset_optimization_scope_report
from agentc._optimizer import Plan
from agentc._patches import _litellm
from agentc._patches._litellm import _wrap_acompletion, _wrap_completion


def _route_contract(requested: str, target: str) -> dict[str, str]:
    return {
        "catalog_version": "test-catalog-v1",
        "price_table_version": "test-prices-v1",
        "provider_protocol": "litellm.completion.v1",
        "provider_namespace": "together_ai",
        "requested_model_id": requested,
        "resolved_requested_model_id": requested,
        "target_model_id": target,
        "target_model_version": f"{target}@test-catalog-v1",
        "target_revision_kind": "catalog_observation",
        "output_token_parameter": "max_tokens",
    }


def _response(model: str = "strong-model") -> SimpleNamespace:
    return SimpleNamespace(
        model=model,
        id="response-1",
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(role="assistant", content="answer"),
            )
        ],
        usage=SimpleNamespace(prompt_tokens=20, completion_tokens=5),
    )


@pytest.fixture(autouse=True)
def _initialized_without_io() -> Any:
    _litellm.unpatch()
    _reset_optimization_scope_report()
    _initialized.set()
    _shutdown_in_progress.clear()
    yield
    _litellm.unpatch()
    _initialized.clear()
    _shutdown_in_progress.clear()


def test_excluded_scope_is_traced_but_never_planned_or_mutated() -> None:
    spans: list[dict[str, Any]] = []
    response = _response("user-model")
    messages = [{"role": "user", "content": "hello"}]
    tools = [{"type": "function", "function": {"name": "lookup"}}]
    request = {
        "model": "user-model",
        "messages": messages,
        "tools": tools,
        "temperature": 0,
    }
    wrapped = MagicMock(return_value=response)

    with (
        optimization_scope("tau2.user_simulator", optimize=False),
        patch(
            "agentc._patches._litellm._write_root_span",
            side_effect=spans.append,
        ),
        patch(
            "agentc._patches._litellm._plan_call",
            wraps=_litellm._plan_call,
        ) as planner,
    ):
        result = _wrap_completion(wrapped, None, (), request)

    assert result is response
    planner.assert_called_once()
    assert planner.call_args.args[2].eligible is False
    wrapped.assert_called_once_with(**request)
    assert wrapped.call_args.kwargs["messages"] is messages
    assert wrapped.call_args.kwargs["tools"] is tools
    attrs = json.loads(spans[0]["attributes"])
    assert attrs["agentc.optimization.scope"] == "tau2.user_simulator"
    assert attrs["agentc.optimization.eligible"] is False
    assert optimization_scope_report()["excluded_calls"] == 1


def test_rewrite_changes_only_optimizer_supported_fields() -> None:
    spans: list[dict[str, Any]] = []
    requested = "together_ai/zai-org/GLM-5.3"
    target = "together_ai/zai-org/GLM-5.3-Flash"
    response = _response(target)
    messages = [{"role": "user", "content": "hello"}]
    tools = [{"type": "function", "function": {"name": "lookup"}}]
    secret = object()
    request = {
        "model": requested,
        "messages": messages,
        "tools": tools,
        "temperature": 0,
        "max_completion_tokens": 200,
        "api_key": secret,
    }
    mutated_call = {
        "model": target,
        "messages": messages,
        "parameters": {
            "temperature": 0,
            "max_output_tokens": 64,
            "extra": {"agentc_routed_target": _route_contract(requested, target)},
        },
    }
    plan = Plan(kind="rewritten", rule="ModelDowngrade", call=mutated_call)
    wrapped = MagicMock(return_value=response)
    events: list[str] = []
    observe = MagicMock(side_effect=lambda *_: events.append("observe"))

    with (
        patch(
            "agentc._patches._litellm._write_root_span",
            side_effect=spans.append,
        ),
        patch(
            "agentc._patches._litellm._plan_call",
            return_value=(plan, "site"),
        ),
        patch("agentc._patches._litellm._observe", new=observe),
        patch(
            "agentc._patches._optimizer_glue.maybe_shadow_record",
            side_effect=lambda *_: events.append("shadow"),
        ),
    ):
        result = _wrap_completion(wrapped, None, (), request)

    assert result is response
    sent = wrapped.call_args.kwargs
    assert sent["model"] == target
    assert sent["max_tokens"] == 64
    assert "max_completion_tokens" not in sent
    assert sent["tools"] is tools
    assert sent["api_key"] is secret
    observe.assert_called_once()
    assert events == ["observe", "shadow"]
    attrs = json.loads(spans[0]["attributes"])
    assert attrs["gen_ai.provider.name"] == "litellm"
    assert attrs["gen_ai.usage.input_tokens"] == 20


def test_positional_model_and_messages_can_be_rewritten() -> None:
    requested = "together_ai/zai-org/GLM-5.3"
    target = "together_ai/zai-org/GLM-5.3-Flash"
    response = _response(target)
    messages = [{"role": "user", "content": "hello"}]
    plan = Plan(
        kind="rewritten",
        rule="ModelDowngrade",
        call={
            "model": target,
            "messages": messages,
            "parameters": {
                "temperature": 0,
                "extra": {"agentc_routed_target": _route_contract(requested, target)},
            },
        },
    )
    wrapped = MagicMock(return_value=response)

    with (
        patch("agentc._patches._litellm._write_root_span"),
        patch("agentc._patches._litellm._plan_call", return_value=(plan, "site")),
        patch("agentc._patches._litellm._observe"),
        patch("agentc._patches._optimizer_glue.maybe_shadow_record"),
    ):
        _wrap_completion(wrapped, None, (requested, messages), {"temperature": 1})

    assert wrapped.call_args.args == (target, messages)
    assert wrapped.call_args.kwargs["temperature"] == 0


@pytest.mark.asyncio
async def test_async_completion_uses_one_plan_and_preserves_response() -> None:
    response = _response()
    wrapped = AsyncMock(return_value=response)
    plan = Plan(kind="pass_through")

    with (
        patch("agentc._patches._litellm._write_root_span"),
        patch(
            "agentc._patches._litellm._plan_call", return_value=(plan, "site")
        ) as planner,
        patch("agentc._patches._litellm._observe") as observe,
    ):
        result = await _wrap_acompletion(
            wrapped,
            None,
            (),
            {"model": "strong-model", "messages": []},
        )

    assert result is response
    planner.assert_called_once()
    wrapped.assert_awaited_once()
    observe.assert_called_once()


@pytest.mark.asyncio
async def test_async_catalog_route_preserves_native_request_shape() -> None:
    requested = "together_ai/zai-org/GLM-5.3"
    target = "together_ai/zai-org/GLM-5.3-Flash"
    messages = [
        {
            "role": "user",
            "content": [{"type": "text", "text": "use the tool"}],
        }
    ]
    tools = [{"type": "function", "function": {"name": "lookup"}}]
    secret = object()
    request = {
        "model": requested,
        "messages": messages,
        "tools": tools,
        "api_key": secret,
    }
    call = {
        "model": target,
        "messages": [],
        "parameters": {
            "max_output_tokens": 64,
            "extra": {
                "agentc_native_messages_opaque": True,
                "agentc_routed_target": _route_contract(requested, target),
            },
        },
    }
    plan = Plan(kind="rewritten", rule="ModelDowngrade", call=call)
    response = _response(target)
    wrapped = AsyncMock(return_value=response)
    events: list[str] = []
    shadow = AsyncMock(side_effect=lambda *_: events.append("shadow"))
    observe = MagicMock(side_effect=lambda *_: events.append("observe"))

    with (
        patch("agentc._patches._litellm._write_root_span"),
        patch("agentc._patches._litellm._plan_call", return_value=(plan, "site")),
        patch("agentc._patches._litellm._observe", new=observe),
        patch(
            "agentc._patches._optimizer_glue.maybe_shadow_record_async",
            new=shadow,
        ),
    ):
        result = await _wrap_acompletion(wrapped, None, (), request)

    assert result is response
    sent = wrapped.await_args.kwargs
    assert sent["model"] == target
    assert sent["messages"] is messages
    assert sent["tools"] is tools
    assert sent["api_key"] is secret
    assert sent["max_tokens"] == 64
    assert plan.dispatch_fallback is False
    assert plan.executed_model_id == target
    shadow.assert_awaited_once()
    assert shadow.await_args.args[:3] == (plan, "site", response)
    assert events == ["observe", "shadow"]


@pytest.mark.asyncio
async def test_async_unsafe_route_falls_back_to_exact_original_once() -> None:
    messages = [{"role": "user", "content": "hello"}]
    tools = [{"type": "function", "function": {"name": "lookup"}}]
    request = {
        "model": "openai/gpt-5.4",
        "messages": messages,
        "tools": tools,
    }
    plan = Plan(
        kind="rewritten",
        rule="ModelDowngrade",
        call={"model": "anthropic/claude-haiku-4-5", "messages": []},
    )
    response = _response("openai/gpt-5.4")
    wrapped = AsyncMock(return_value=response)

    with (
        patch("agentc._patches._litellm._write_root_span"),
        patch("agentc._patches._litellm._plan_call", return_value=(plan, "site")),
        patch("agentc._patches._litellm._observe"),
    ):
        result = await _wrap_acompletion(wrapped, None, (), request)

    assert result is response
    wrapped.assert_awaited_once_with(**request)
    assert wrapped.await_args.kwargs["messages"] is messages
    assert wrapped.await_args.kwargs["tools"] is tools
    assert plan.dispatch_fallback is True
    assert plan.dispatch_fallback_reason == "mutated_dispatch_failed"
    assert plan.executed_model_id == "openai/gpt-5.4"


def test_observation_accounts_for_litellm_usage_and_known_model_cost() -> None:
    plan = Plan(kind="rewritten", rule="OutputBudget")
    response = _response("gpt-4o-mini-2024-07-18")

    with patch("agentc._optimizer.observe_outcome") as observe:
        _litellm._observe(
            plan,
            response,
            "site",
            {"model": "gpt-4o-mini-2024-07-18", "messages": []},
            0.25,
        )

    outcome = observe.call_args.args[1]
    assert outcome["input_tokens"] == 20
    assert outcome["output_tokens"] == 5
    assert outcome["latency_ms"] == 250
    assert outcome["cost_usd"] > 0
    assert outcome["dispatch_fallback"] is False
    assert outcome["executed_model_id"] == "gpt-4o-mini-2024-07-18"


def test_litellm_owns_nested_openai_and_anthropic_sdk_calls() -> None:
    from agentc._patches._anthropic import _wrap_create as anthropic_create
    from agentc._patches._openai import _wrap_create as openai_create

    response = _response()
    nested = {"openai": 0, "anthropic": 0}

    def raw_openai(*args: Any, **kwargs: Any) -> SimpleNamespace:
        nested["openai"] += 1
        return response

    def raw_anthropic(*args: Any, **kwargs: Any) -> SimpleNamespace:
        nested["anthropic"] += 1
        return response

    def outer(*args: Any, **kwargs: Any) -> SimpleNamespace:
        openai_create(raw_openai, None, (), kwargs)
        anthropic_create(raw_anthropic, None, (), kwargs)
        return response

    plan = Plan(kind="pass_through")
    spans: list[dict[str, Any]] = []
    with (
        patch(
            "agentc._patches._litellm._write_root_span",
            side_effect=spans.append,
        ),
        patch(
            "agentc._patches._litellm._plan_call",
            return_value=(plan, "site"),
        ) as planner,
        patch("agentc._patches._litellm._observe"),
    ):
        _wrap_completion(
            outer,
            None,
            (),
            {"model": "strong-model", "messages": []},
        )

    assert nested == {"openai": 1, "anthropic": 1}
    planner.assert_called_once()
    assert len(spans) == 1


def test_user_exception_is_traced_and_propagated_once() -> None:
    spans: list[dict[str, Any]] = []
    calls = 0

    def failing(*args: Any, **kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("provider failed")

    with (
        patch(
            "agentc._patches._litellm._write_root_span",
            side_effect=spans.append,
        ),
        patch(
            "agentc._patches._litellm._plan_call",
            return_value=(Plan(kind="pass_through"), "site"),
        ),
    ):
        with pytest.raises(RuntimeError, match="provider failed"):
            _wrap_completion(
                failing,
                None,
                (),
                {"model": "strong-model", "messages": []},
            )

    assert calls == 1
    assert spans[0]["status"] == "ERROR"


def test_patch_repairs_loaded_alias_and_unpatch_restores_exact_functions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    litellm = ModuleType("litellm")
    main = ModuleType("litellm.main")
    tau_utils = ModuleType("tau2.utils.llm_utils")

    def completion(*args: Any, **kwargs: Any) -> str:
        return "sync"

    async def acompletion(*args: Any, **kwargs: Any) -> str:
        return "async"

    litellm.completion = completion  # type: ignore[attr-defined]
    litellm.acompletion = acompletion  # type: ignore[attr-defined]
    main.completion = completion  # type: ignore[attr-defined]
    main.acompletion = acompletion  # type: ignore[attr-defined]
    tau_utils.completion = completion  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "litellm", litellm)
    monkeypatch.setitem(sys.modules, "litellm.main", main)
    monkeypatch.setitem(sys.modules, "tau2.utils.llm_utils", tau_utils)

    _litellm.patch()
    assert litellm.completion is not completion  # type: ignore[attr-defined]
    assert main.completion is litellm.completion  # type: ignore[attr-defined]
    assert tau_utils.completion is litellm.completion  # type: ignore[attr-defined]

    _litellm.unpatch()
    assert litellm.completion is completion  # type: ignore[attr-defined]
    assert litellm.acompletion is acompletion  # type: ignore[attr-defined]
    assert main.completion is completion  # type: ignore[attr-defined]
    assert main.acompletion is acompletion  # type: ignore[attr-defined]
    assert tau_utils.completion is completion  # type: ignore[attr-defined]


def test_unpatch_repairs_alias_imported_after_patch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    litellm = ModuleType("litellm")
    main = ModuleType("litellm.main")

    def completion(*args: Any, **kwargs: Any) -> str:
        return "sync"

    async def acompletion(*args: Any, **kwargs: Any) -> str:
        return "async"

    litellm.completion = completion  # type: ignore[attr-defined]
    litellm.acompletion = acompletion  # type: ignore[attr-defined]
    main.completion = completion  # type: ignore[attr-defined]
    main.acompletion = acompletion  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "litellm", litellm)
    monkeypatch.setitem(sys.modules, "litellm.main", main)

    _litellm.patch()
    tau_utils = ModuleType("tau2.utils.llm_utils")
    tau_utils.completion = litellm.completion  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tau2.utils.llm_utils", tau_utils)

    _litellm.unpatch()
    assert tau_utils.completion is completion  # type: ignore[attr-defined]


def test_patch_is_fail_open_for_unexpected_import_error() -> None:
    with patch(
        "agentc._patches._litellm.importlib.import_module",
        side_effect=RuntimeError("broken optional dependency"),
    ):
        _litellm.patch()

    assert _litellm._patched is False
    assert _litellm._patches == []


def test_partial_patch_failure_restores_installed_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    litellm = ModuleType("litellm")
    main = ModuleType("litellm.main")

    def completion(*args: Any, **kwargs: Any) -> str:
        return "sync"

    async def acompletion(*args: Any, **kwargs: Any) -> str:
        return "async"

    litellm.completion = completion  # type: ignore[attr-defined]
    litellm.acompletion = acompletion  # type: ignore[attr-defined]
    main.completion = completion  # type: ignore[attr-defined]
    main.acompletion = acompletion  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "litellm", litellm)
    monkeypatch.setitem(sys.modules, "litellm.main", main)

    original_install = _litellm._install_target
    installs = 0

    def fail_second_install(module: ModuleType, attribute: str, wrapper: Any) -> None:
        nonlocal installs
        installs += 1
        if installs == 2:
            raise RuntimeError("partial installation")
        original_install(module, attribute, wrapper)

    with patch(
        "agentc._patches._litellm._install_target",
        side_effect=fail_second_install,
    ):
        _litellm.patch()

    assert _litellm._patched is False
    assert _litellm._patches == []
    assert litellm.completion is completion  # type: ignore[attr-defined]
    assert litellm.acompletion is acompletion  # type: ignore[attr-defined]
