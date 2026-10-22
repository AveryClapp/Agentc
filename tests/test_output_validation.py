"""Actual release gating, usage accounting and callback isolation."""

import asyncio
import importlib
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agentc._dispatch_context import current_provider_dispatch
from agentc._executor import dispatch
from agentc._optimization_scope import decide_optimization
from agentc._optimizer import Plan
from agentc._output_validation import (
    ValidationContract,
    ValidationReceipt,
    bind_validation_request,
    experimental_validation_scope,
)
from agentc._patches._optimizer_glue import build_outcome_openai, dispatch_sync


def response(text):
    return SimpleNamespace(
        model="gpt-4o-2024-11-20",
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        content=[SimpleNamespace(text=text)],
        usage=SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=50,
            input_tokens=100,
            output_tokens=50,
            cost=0.005,
            is_byok=False,
        ),
    )


def contract(callback, **kwargs):
    return ValidationContract(
        "fixture-v1", "a" * 64, "validator-pinned-v1", 0.002, callback, **kwargs
    )


def receipt(request, accepted=True, **kwargs):
    return replace(
        ValidationReceipt(
            request.execution_id,
            request.contract_sha256,
            "validator-pinned-v1",
            accepted,
            7,
            3,
            0.001,
        ),
        **kwargs,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("async_path", [False, True])
@pytest.mark.parametrize("kind", ["cached", "rewritten", "composed"])
@pytest.mark.parametrize(
    "verdict", ["accept", "reject", "unknown", "stale", "model", "over_cap"]
)
async def test_release_and_complete_spend(async_path, kind, verdict):
    calls, validations = [], []
    candidate, reference = response("candidate"), response("reference")
    plan = Plan(kind=kind, call={"model": candidate.model})
    request_kwargs = {
        "model": reference.model,
        "messages": [
            {"role": "tool", "tool_call_id": "original-id", "content": "result"}
        ],
        "api_key": object(),
        "http_client": object(),
    }

    def validate(request):
        validations.append(request)
        assert (
            json.loads(request.request_json)["messages"][0]["tool_call_id"]
            == "original-id"
        )
        assert (
            "api_key" not in request.request_json
            and "http_client" not in request.request_json
        )
        assert request.candidate_text == "candidate"
        assert current_provider_dispatch().kind == "validation"
        assert decide_optimization().eligible is False
        modifications = {
            "accept": {},
            "reject": {"accepted": False},
            "unknown": {"accepted": None},
            "stale": {"execution_id": "old-execution"},
            "model": {"model_version": "wrong"},
            "over_cap": {"cost_usd": 0.003},
        }[verdict]
        return receipt(request, **modifications)

    def original():
        calls.append("reference")
        return reference

    def mutated(_call):
        calls.append("candidate")
        return candidate

    async def original_async():
        return original()

    async def mutated_async(call):
        return mutated(call)

    with experimental_validation_scope(contract(validate)):
        bind_validation_request(plan, request_kwargs)
        arguments = dict(decode_cached=lambda _value: candidate)
        result = (
            await dispatch(
                plan,
                run_original=original_async,
                run_mutated=mutated_async,
                **arguments,
            )
            if async_path
            else dispatch_sync(
                plan, run_original=original, run_mutated=mutated, **arguments
            )
        )
    accepted = verdict == "accept"
    assert result is (candidate if accepted else reference)
    assert calls == ([] if kind == "cached" else ["candidate"]) + (
        [] if accepted else ["reference"]
    )
    assert len(validations) == 1
    assert plan.validation_rejected is (not accepted)
    outcome = build_outcome_openai(
        result, elapsed_s=0.02, model=result.model, call_site_id="release", plan=plan
    )
    expected_cost = 0.003 if verdict == "over_cap" else 0.001
    if not accepted and kind != "cached":
        expected_cost += 0.005
    assert outcome["additional_inference"]["cost_usd"] == pytest.approx(expected_cost)
    assert outcome["cost_usd"] == (0.0 if kind == "cached" and accepted else 0.005)
    assert outcome["additional_inference"]["input_tokens"] == (
        107 if not accepted and kind != "cached" else 7
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("async_path", [False, True])
@pytest.mark.parametrize(
    "failure", [RuntimeError, KeyboardInterrupt, asyncio.CancelledError]
)
async def test_validator_failure_preserves_fatal_stops_and_rejects_unknown_billing(
    async_path, failure
):
    calls = []
    plan = Plan(kind="cached")

    def validate(_request):
        raise failure()

    def original():
        calls.append("reference")
        return response("reference")

    async def original_async():
        return original()

    with experimental_validation_scope(contract(validate)):
        bind_validation_request(plan, {"model": "gpt-4o", "messages": []})
        if failure is RuntimeError:
            if async_path:
                await dispatch(
                    plan,
                    run_original=original_async,
                    run_mutated=None,
                    decode_cached=lambda _: response("candidate"),
                )
            else:
                dispatch_sync(
                    plan,
                    run_original=original,
                    run_mutated=None,
                    decode_cached=lambda _: response("candidate"),
                )
            assert calls == ["reference"]
            assert plan.validation_status == "unmeasured"
            assert plan.inference_cost_complete is False
        else:
            with pytest.raises(failure):
                if async_path:
                    await dispatch(
                        plan,
                        run_original=original_async,
                        run_mutated=None,
                        decode_cached=lambda _: response("candidate"),
                    )
                else:
                    dispatch_sync(
                        plan,
                        run_original=original,
                        run_mutated=None,
                        decode_cached=lambda _: response("candidate"),
                    )
            assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("vendor", ["openai", "anthropic", "litellm"])
@pytest.mark.parametrize("async_path", [False, True])
@pytest.mark.parametrize("accepted", [False, True])
async def test_actual_six_sdk_adapters_validate_cached_results(
    monkeypatch, vendor, async_path, accepted
):
    module = importlib.import_module("agentc._patches._" + vendor)
    names = {
        "openai": ("_plan_openai_call", "_wrap_create", "_wrap_create_async"),
        "anthropic": ("_plan_anthropic_call", "_wrap_create", "_wrap_create_async"),
        "litellm": ("_plan_call", "_wrap_completion", "_wrap_acompletion"),
    }
    planner, sync_name, async_name = names[vendor]
    plan = Plan(kind="cached")
    candidate, reference = response("candidate"), response("reference")
    monkeypatch.setattr(module, planner, lambda *_args: (plan, "sdk-validation"))
    monkeypatch.setattr(module, "_is_initialized", lambda: True)
    monkeypatch.setattr(module, "_write_root_span", lambda *_args, **_kwargs: None)
    calls = []

    def original(*_args, **_kwargs):
        calls.append("reference")
        return reference

    async def original_async(*args, **kwargs):
        return original(*args, **kwargs)

    with experimental_validation_scope(
        contract(lambda request: receipt(request, accepted))
    ):
        with (
            patch.object(module, "_emit_span"),
            patch(
                "agentc._patches._optimizer_glue.decode_cached_response",
                return_value=candidate,
            ),
            patch("agentc._optimizer.observe_outcome") as observe,
        ):
            wrapper = getattr(module, async_name if async_path else sync_name)
            kwargs = {
                "model": reference.model,
                "messages": [{"role": "user", "content": "task"}],
                "max_tokens": 128,
            }
            result = (
                await wrapper(original_async, None, (), kwargs)
                if async_path
                else wrapper(original, None, (), kwargs)
            )
    assert result is (candidate if accepted else reference)
    assert calls == ([] if accepted else ["reference"])
    assert plan.validation_status == ("accepted" if accepted else "rejected")
    assert observe.call_count == 1
    assert observe.call_args.args[1]["additional_inference"]["cost_usd"] == 0.001


def test_contract_change_cannot_rebind_an_already_planned_execution():
    first = contract(lambda request: receipt(request))
    plan = Plan(kind="cached")
    with experimental_validation_scope(first):
        bind_validation_request(plan, {"messages": []})
    with experimental_validation_scope(replace(first, model_version="changed-version")):
        bind_validation_request(plan, {"messages": []})
        result = dispatch_sync(
            plan,
            run_original=lambda: "reference",
            run_mutated=None,
            decode_cached=lambda _: response("candidate"),
        )
    assert result == "reference"
    assert plan.validation_status == "unsupported"


def test_failed_reference_fallback_is_not_retried():
    calls = []
    plan = Plan(kind="rewritten", call={"model": "gpt-4o"})

    def original():
        calls.append("reference")
        raise RuntimeError("reference failed")

    with experimental_validation_scope(
        contract(lambda request: receipt(request, False))
    ):
        bind_validation_request(plan, {"messages": []})
        with pytest.raises(RuntimeError, match="reference failed"):
            dispatch_sync(
                plan, run_original=original, run_mutated=lambda _: response("candidate")
            )
    assert calls == ["reference"]


def test_rejected_candidate_missing_usage_excludes_economics_and_still_falls_back():
    plan = Plan(kind="rewritten", call={"model": "gpt-4o"})
    candidate = response("candidate")
    candidate.usage = None
    with experimental_validation_scope(
        contract(lambda request: receipt(request, False))
    ):
        bind_validation_request(plan, {"messages": []})
        result = dispatch_sync(
            plan, run_original=lambda: "reference", run_mutated=lambda _: candidate
        )
    assert result == "reference"
    assert plan.inference_cost_complete is False
    assert plan.additional_inference["cost_usd"] == 0.001


@pytest.mark.asyncio
async def test_async_validator_uses_async_callback_and_preserves_task_context():
    from agentc._output_validation import validate_candidate_async

    def sync(_request):
        pytest.fail("async callback must take precedence")

    async def asynchronous(request):
        await asyncio.sleep(0)
        assert current_provider_dispatch().kind == "validation"
        assert not decide_optimization().eligible
        return receipt(request)

    plan = Plan(kind="cached")
    with experimental_validation_scope(contract(sync, validate_async=asynchronous)):
        bind_validation_request(plan, {"messages": []})
        assert await validate_candidate_async(plan, response("candidate"))
    assert current_provider_dispatch() is None


@pytest.mark.parametrize(
    "changes",
    [
        {"input_tokens": True},
        {"input_tokens": -1},
        {"output_tokens": 2**32},
        {"cost_usd": float("nan")},
        {"cost_usd": -0.001},
        {"cost_usd": True},
        {"cost_usd": 10**1000},
    ],
)
def test_invalid_validator_usage_never_becomes_measured_evidence(changes):
    from agentc._output_validation import validate_candidate

    plan = Plan(kind="cached")
    with experimental_validation_scope(
        contract(lambda request: receipt(request, **changes))
    ):
        bind_validation_request(plan, {"messages": []})
        assert validate_candidate(plan, response("candidate")) is False
    assert plan.inference_cost_complete is False
    assert plan.additional_inference is None


@pytest.mark.asyncio
@pytest.mark.parametrize("async_path", [False, True])
async def test_rejection_persistence_failure_stops_without_a_reference_retry(
    async_path,
):
    from agentc._output_validation import ValidationPersistenceError

    calls = []
    plan = Plan(kind="cached", source_call_json="issued")

    def original():
        calls.append("reference")
        return response("reference")

    async def original_async():
        return original()

    with experimental_validation_scope(
        contract(lambda request: receipt(request, False))
    ):
        bind_validation_request(plan, {"messages": []})
        with (
            patch(
                "agentc._native.optimize_record_output_validation_rejection",
                return_value=False,
            ),
            pytest.raises(ValidationPersistenceError),
        ):
            if async_path:
                await dispatch(
                    plan,
                    run_original=original_async,
                    run_mutated=None,
                    decode_cached=lambda _: response("candidate"),
                )
            else:
                dispatch_sync(
                    plan,
                    run_original=original,
                    run_mutated=None,
                    decode_cached=lambda _: response("candidate"),
                )
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("async_path", [False, True])
async def test_fallback_duration_is_measured_and_unknown_candidate_spend_is_excluded(
    async_path,
):
    calls = []
    plan = Plan(kind="rewritten", call={"model": "gpt-4o"})

    def original():
        calls.append("reference")
        return response("reference")

    def mutated(_call):
        raise RuntimeError("provider failed after dispatch")

    async def original_async():
        return original()

    async def mutated_async(call):
        return mutated(call)

    with experimental_validation_scope(contract(lambda request: receipt(request))):
        bind_validation_request(plan, {"messages": []})
        result = (
            await dispatch(plan, run_original=original_async, run_mutated=mutated_async)
            if async_path
            else dispatch_sync(plan, run_original=original, run_mutated=mutated)
        )
    assert calls == ["reference"]
    assert plan.inference_cost_complete is False
    assert plan.validation_rejected
    assert plan.reference_fallback_latency_ms >= 0
    outcome = build_outcome_openai(
        result,
        elapsed_s=1.0,
        model=result.model,
        call_site_id="failed-candidate",
        plan=plan,
    )
    assert (
        outcome["reference_fallback_latency_ms"] == plan.reference_fallback_latency_ms
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("async_path", [False, True])
async def test_failure_reporting_cannot_replace_the_original_cancellation(async_path):
    class ReportingStopped(BaseException):
        pass

    def stop(_request):
        raise asyncio.CancelledError("original cancellation")

    def reporting(_request):
        raise ReportingStopped("failure reporter stopped")

    plan = Plan(kind="cached")
    configured = replace(contract(stop), failure_accounting=reporting)
    with experimental_validation_scope(configured):
        bind_validation_request(plan, {"messages": []})
        with pytest.raises(asyncio.CancelledError, match="original cancellation"):
            if async_path:
                await dispatch(
                    plan,
                    run_original=None,
                    run_mutated=None,
                    decode_cached=lambda _: response("candidate"),
                )
            else:
                dispatch_sync(
                    plan,
                    run_original=None,
                    run_mutated=None,
                    decode_cached=lambda _: response("candidate"),
                )
    assert not plan.inference_cost_complete
