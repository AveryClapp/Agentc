"""Declared template revisions across contexts, SDKs and native admission."""

import asyncio
import importlib
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from copy import deepcopy
from unittest.mock import patch

import pytest

import agentc
from agentc import _native, application_version
from agentc._application_version import CONTEXT_KEY, VERSION, declaration
from agentc._optimizer import Plan, observe_outcome, plan_call
from agentc._patches._optimizer_glue import maybe_explore_record
from test_cached_exploration_native import (
    PROTOCOLS,
    response,
    restart,
    runtime as runtime,
    seeded_call,
)
from test_provider_config_native import request


@pytest.mark.parametrize(
    "invalid", [None, 1, True, "", "a" * 129, "private prompt", "revision\n", "é"]
)
def test_invalid_revisions_never_enter_context(invalid):
    with pytest.raises(ValueError):
        with application_version(invalid):
            pytest.fail("invalid revision entered context")
    assert declaration() is None


def test_nested_exception_restores_revision_and_declarations_are_copies():
    assert declaration() is None
    with application_version("support-prompts-v1"):
        first = declaration()
        assert "support-prompts" not in json.dumps(first)
        with pytest.raises(RuntimeError):
            with application_version("support-prompts-v2"):
                assert declaration() != first
                raise RuntimeError("exit")
        assert declaration() == first
        altered = declaration()
        altered["sha256"] = "b" * 64
        assert declaration() == first
    assert declaration() is None


@pytest.mark.asyncio
async def test_async_revisions_are_isolated_and_inherited_by_owned_threads():
    async def child(version):
        with application_version(version):
            before = declaration()
            await asyncio.sleep(0)
            assert await asyncio.to_thread(declaration) == before
            return declaration()

    first, second = await asyncio.gather(child("revision-v1"), child("revision-v2"))
    assert first != second and declaration() is None


def test_parallel_map_propagates_independent_revision_contexts():
    with application_version("parent-v1"):
        expected = declaration()

        def child(index):
            before = declaration()
            with application_version("child-" + str(index)):
                nested = declaration()
            assert declaration() == before
            return before, nested

        values = agentc.parallel_map(child, range(8), max_workers=4)
        assert all(before == expected for before, _ in values)
        assert len({nested["sha256"] for _, nested in values}) == 8
        with agentc.traced_executor(ThreadPoolExecutor(max_workers=2)) as pool:
            assert pool.submit(declaration).result() == expected
    assert declaration() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("vendor", ["openai", "anthropic", "litellm"])
@pytest.mark.parametrize("async_path", [False, True])
async def test_all_six_sdk_paths_bind_revision_without_sending_it_to_provider(
    monkeypatch, vendor, async_path
):
    module = importlib.import_module("agentc._patches._" + vendor)
    monkeypatch.setattr(module, "_is_initialized", lambda: True)
    monkeypatch.setattr(module, "_emit_span", lambda **_kwargs: None)
    observer = {
        "openai": "_observe_openai_outcome",
        "anthropic": "_observe_anthropic_outcome",
        "litellm": "_observe",
    }[vendor]
    monkeypatch.setattr(module, observer, lambda *_args, **_kwargs: None)
    captured = []
    sent = []

    def plan(call):
        captured.append(call)
        return Plan(kind="pass_through")

    def original(*_args, **kwargs):
        sent.append(deepcopy(kwargs))
        return response(kwargs["model"])

    async def original_async(*args, **kwargs):
        return original(*args, **kwargs)

    kwargs = {
        "model": "claude-sonnet-4-5-20250929"
        if vendor == "anthropic"
        else "gpt-4o-2024-11-20",
        "max_tokens": 512,
        "messages": [{"role": "user", "content": "private input"}],
    }
    sync_name, async_name = (
        ("_wrap_completion", "_wrap_acompletion")
        if vendor == "litellm"
        else ("_wrap_create", "_wrap_create_async")
    )
    with (
        application_version("support-prompts-v2"),
        patch("agentc._optimizer.plan_call", side_effect=plan),
    ):
        expected = declaration()
        wrapper = getattr(module, async_name if async_path else sync_name)
        if async_path:
            await wrapper(original_async, None, (), kwargs)
        else:
            wrapper(original, None, (), kwargs)
    assert len(captured) == 1
    assert (
        captured[0]["parameters"]["extra"]["agentc_route_context"][CONTEXT_KEY]
        == expected
    )
    assert sent == [kwargs]


def test_older_native_cannot_ignore_a_declared_revision(monkeypatch):
    monkeypatch.setattr(_native, "optimize_application_version", lambda: "older-v0")
    # An unscoped application retains its existing identity contract.
    request(PROTOCOLS[0])
    with (
        application_version("support-v2"),
        pytest.raises(RuntimeError, match="application version"),
    ):
        request(PROTOCOLS[0])


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_actual_cache_admission_cold_starts_for_new_revision_and_survives_restart(
    runtime, protocol
):
    with application_version("support-v1"):
        call, builder = seeded_call(runtime, protocol)
        for _ in range(20):
            plan = plan_call(call)
            assert plan.kind == "pass_through" and plan.counterfactual.kind == "cached"
            reference = response(call["model"])
            assert observe_outcome(
                plan,
                builder(
                    reference,
                    elapsed_s=0.1,
                    model=reference.model,
                    call_site_id=call["call_site_id"],
                    plan=plan,
                ),
            )

            def extract(candidate, cached, elapsed):
                return builder(
                    cached,
                    elapsed_s=elapsed,
                    model=cached.model,
                    call_site_id=call["call_site_id"],
                    plan=candidate,
                )

            worker = maybe_explore_record(
                plan,
                reference,
                lambda _: pytest.fail("cached comparison dispatched"),
                extract,
            )
            assert worker is not None
            worker.join(5)
            assert not worker.is_alive() and plan.exploration_lease_token is None
        restart(runtime)
        admitted = plan_call(call)
        assert admitted.kind == "cached"
        first_key = json.loads(admitted.raw_json)["agentc_observation_context"]["key"]
    for revision in (None, "support-v2"):
        with application_version(revision) if revision else nullcontext():
            changed, _ = seeded_call(runtime, protocol)
            selected = plan_call(changed)
            assert selected.kind == "pass_through"
            context = json.loads(selected.raw_json)["agentc_observation_context"]
            assert context["key"]["call_site_version"] != first_key["call_site_version"]
            restart(runtime)
            assert plan_call(changed).kind == "pass_through"
    # Old cohorts remain intact, but only the same declaration can use them.
    with application_version("support-v1"):
        assert plan_call(call).kind == "cached"


@pytest.mark.parametrize("compose", ["0", "1"])
@pytest.mark.parametrize(
    "invalid",
    [
        None,
        {"version": "future", "sha256": "a" * 64},
        {"version": VERSION, "sha256": "bad"},
        {"version": VERSION, "sha256": "a" * 64, "extra": True},
    ],
)
def test_malformed_revision_never_obtains_a_native_identity(
    runtime, monkeypatch, compose, invalid
):
    monkeypatch.setenv("AGENTC_COMPOSE", compose)
    monkeypatch.setenv("AGENTC_EVAL_PLANNER_MODE", "current_greedy")
    _native.optimize_reset()
    _native.optimize_configure(str(runtime))
    call = request(PROTOCOLS[0])
    call["parameters"]["extra"]["agentc_route_context"][CONTEXT_KEY] = invalid
    for _ in range(5):
        result = _native.optimize_plan(json.dumps(call))
        assert json.loads(result) == {"kind": "pass_through"}
