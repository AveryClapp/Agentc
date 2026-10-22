"""Exact invocation kinds and opaque native tokens at the LiteLLM provider seam."""

from contextvars import ContextVar
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agentc import optimization_scope
from agentc._dispatch_context import current_provider_dispatch, provider_dispatch
from agentc._lifecycle import _initialized, _shutdown_in_progress
from agentc._optimizer import Plan
from agentc._patches._litellm import _wrap_acompletion, _wrap_completion
from agentc._patches._optimizer_glue import drain_exploration


def response():
    return SimpleNamespace(
        model="openai/gpt-4o",
        choices=[SimpleNamespace(message=SimpleNamespace(content="answer"))],
        usage=SimpleNamespace(prompt_tokens=2, completion_tokens=1),
    )


def request():
    return {
        "model": "openai/gpt-4o",
        "messages": [{"role": "user", "content": "fixture"}],
        "max_tokens": 8,
    }


def candidate():
    return Plan(
        kind="rewritten",
        rule="OutputBudget",
        observation_token="candidate-token",
        call={
            "model": "openai/gpt-4o",
            "messages": [{"role": "user", "content": "fixture"}],
            "parameters": {"max_output_tokens": 4},
        },
    )


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    _initialized.set()
    _shutdown_in_progress.clear()
    monkeypatch.setenv("AGENTC_OPTIMIZE_SHADOW", "0")
    with (
        patch("agentc._patches._litellm._write_root_span"),
        patch("agentc._patches._litellm._observe"),
    ):
        yield
    drain_exploration(2000)
    _initialized.clear()


def test_nested_context_restores_and_does_not_assign_primary_token_to_reference():
    plan = Plan(kind="rewritten", observation_token="primary")
    assert current_provider_dispatch() is None
    with provider_dispatch("primary", plan=plan):
        assert current_provider_dispatch().observation_token == "primary"
        with provider_dispatch("shadow", primary_plan=plan):
            context = current_provider_dispatch()
            assert context.observation_token is None
            assert context.parent_observation_token == "primary"
        assert current_provider_dispatch().kind == "primary"
    assert current_provider_dispatch() is None


def test_unobserved_native_plan_is_preserved_without_inventing_a_handle():
    raw = '{"kind":"pass_through","agentc_observation_context":{"issued":"native"}}'
    plan = Plan(kind="pass_through", raw_json=raw)
    with provider_dispatch("primary", plan=plan):
        context = current_provider_dispatch()
        assert context.native_plan_json == raw
        assert context.observation_token is None
    with provider_dispatch("shadow", primary_plan=plan):
        context = current_provider_dispatch()
        assert context.native_plan_json is None
        assert context.parent_native_plan_json == raw


def test_explicit_physical_role_plan_keeps_its_logical_owner():
    owner = Plan(kind="pass_through", raw_json='{"owner":"native"}')
    dispatched = Plan(kind="pass_through", raw_json='{"dispatch":"native"}')
    for role in ("fallback", "validation", "shadow", "exploration"):
        with provider_dispatch(role, primary_plan=owner, dispatch_plan=dispatched):
            context = current_provider_dispatch()
            assert context.native_plan_json == dispatched.raw_json
            assert context.parent_native_plan_json == owner.raw_json
            assert context.observation_token is None


@pytest.mark.asyncio
@pytest.mark.parametrize("async_path", [False, True])
async def test_validator_sdk_invocation_keeps_its_validation_role(async_path):
    plan = Plan(kind="cached", raw_json='{"kind":"cached","value":"parent"}')
    observed = []

    def wrapped(**kwargs):
        observed.append(current_provider_dispatch())
        return response()

    async def wrapped_async(**kwargs):
        return wrapped(**kwargs)

    with optimization_scope("output-validation", optimize=False), provider_dispatch(
        "validation", primary_plan=plan
    ), patch("agentc._patches._litellm._plan_call", return_value=(None, "site")):
        if async_path:
            await _wrap_acompletion(wrapped_async, None, (), request())
        else:
            _wrap_completion(wrapped, None, (), request())
    assert len(observed) == 1
    assert observed[0].kind == "validation"
    assert observed[0].parent_native_plan_json == plan.raw_json


def test_background_probe_keeps_actor_task_scope_after_parent_returns():
    marker = ContextVar("fixture_task", default="missing")
    entered, release = threading.Event(), threading.Event()
    contexts, markers, completed = [], [], []
    plan = Plan(
        kind="pass_through",
        observation_token="source-token",
        exploration_lease_token="lease-token",
        counterfactual=candidate(),
    )

    def wrapped(**kwargs):
        context = current_provider_dispatch()
        if context.kind == "exploration":
            entered.set()
            assert release.wait(2)
        contexts.append(context)
        markers.append(marker.get())
        return response()

    with (
        patch("agentc._patches._litellm._plan_call", return_value=(plan, "site")),
        patch(
            "agentc._optimizer.complete_exploration",
            side_effect=lambda *args: completed.append(args) or True,
        ),
    ):
        token = marker.set("actual-task")
        try:
            with optimization_scope("tau2.evaluated_assistant", optimize=True):
                result = _wrap_completion(wrapped, None, (), request())
            assert result.choices[0].message.content == "answer"
        finally:
            marker.reset(token)
        assert entered.wait(2)
        release.set()
        drain_exploration(2000)
    assert markers == ["actual-task", "actual-task"]
    assert [context.kind for context in contexts] == ["primary", "exploration"]
    assert contexts[0].observation_token == "source-token"
    assert contexts[1].observation_token == "candidate-token"
    assert contexts[1].parent_observation_token == "source-token"
    assert contexts[1].exploration_lease_token == "lease-token"
    assert len(completed) == 1


@pytest.mark.parametrize("fallback", [False, True])
def test_sync_shadow_and_fallback_reference_calls_have_explicit_identity(
    fallback, monkeypatch
):
    plan = candidate()
    contexts = []

    def wrapped(**kwargs):
        context = current_provider_dispatch()
        contexts.append(context)
        if fallback and kwargs["max_tokens"] == 4:
            raise RuntimeError("ordinary dispatch error")
        return response()

    monkeypatch.setenv("AGENTC_OPTIMIZE_SHADOW", "1")
    with (
        patch("agentc._patches._litellm._plan_call", return_value=(plan, "site")),
        patch("agentc._optimizer.record_divergence"),
    ):
        _wrap_completion(wrapped, None, (), request())
    assert [context.kind for context in contexts] == [
        "primary",
        "fallback" if fallback else "shadow",
    ]
    assert contexts[1].observation_token is None
    assert contexts[1].parent_observation_token == "candidate-token"


@pytest.mark.asyncio
async def test_async_shadow_context_has_the_same_attribution(monkeypatch):
    contexts = []
    plan = candidate()

    async def wrapped(**kwargs):
        contexts.append(current_provider_dispatch())
        return response()

    monkeypatch.setenv("AGENTC_OPTIMIZE_SHADOW", "1")
    with (
        patch("agentc._patches._litellm._plan_call", return_value=(plan, "site")),
        patch("agentc._optimizer.record_divergence"),
    ):
        await _wrap_acompletion(wrapped, None, (), request())
    assert [context.kind for context in contexts] == ["primary", "shadow"]
    assert contexts[1].observation_token is None
    assert contexts[1].parent_observation_token == "candidate-token"
