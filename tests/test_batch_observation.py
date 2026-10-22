"""Ordered map receipt completeness and preservation of application behavior."""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import agentc
from agentc._batch_observation import note_outcome, note_plan, note_sdk_call
from agentc._parallel import get_parallel_peer


def plan() -> SimpleNamespace:
    return SimpleNamespace(
        raw_json='{"kind":"pass_through"}',
        source_call_json='{"fixture":true}',
        counterfactual=None,
    )


def outcome() -> dict:
    return {"cost_source": "provider_reported", "cost_usd": 0.01}


def native_record(receipt_json: str) -> str:
    receipt = json.loads(receipt_json)
    return json.dumps(
        {
            "status": "recorded",
            "admission": "observe_only",
            "totals": {
                "input_tokens": 6,
                "output_tokens": 3,
                "cost_usd": len(receipt["calls"]) * 0.01,
            },
            "key": {"call_site_version": "a" * 64, "execution_plan_id": "b" * 64},
        }
    )


def record_call() -> SimpleNamespace:
    note_sdk_call()
    value = plan()
    note_plan(value)
    note_outcome(value, outcome())
    return value


def test_out_of_order_completion_and_multiple_calls_keep_driver_positions():
    first = threading.Event()
    captured, observations = [], []

    def fn(item):
        if item == 0:
            assert first.wait(2)
        record_call()
        if item == 1:
            record_call()
            first.set()
        return item

    def native(receipt):
        captured.append(json.loads(receipt))
        return native_record(receipt)

    with (
        agentc.application_version("release-v1"),
        patch.object(
            agentc._native,
            "optimize_observe_multi_call",
            side_effect=native,
            create=True,
        ),
    ):
        assert agentc.parallel_map(fn, [0, 1], on_observation=observations.append) == [
            0,
            1,
        ]
    assert len(captured) == 1
    receipt = captured[0]
    assert [call["position"] for call in receipt["calls"]] == [
        {"item": 0, "call": 0},
        {"item": 1, "call": 0},
        {"item": 1, "call": 1},
    ]
    assert receipt["max_workers"] == 2
    assert observations[0].status == "recorded"
    assert observations[0].sdk_calls == observations[0].observed_calls == 3
    assert observations[0].cost_usd == 0.03
    assert "fixture" not in repr(observations[0])


@pytest.mark.parametrize(
    "failure", ["missing", "duplicate", "bypass", "probe", "invalid"]
)
def test_partial_or_auxiliary_work_abstains_without_native_mutation(failure):
    observations = []

    def fn(item):
        value = record_call()
        if item == 1:
            if failure == "bypass":
                note_sdk_call()
            elif failure == "duplicate":
                note_outcome(value, outcome())
            elif failure == "probe":
                value.counterfactual = plan()
            else:
                note_sdk_call()
                value = plan()
                note_plan(value)
                if failure == "invalid":
                    note_outcome(value, {"cost_usd": float("nan")})
        return item

    with (
        agentc.application_version("release-v1"),
        patch.object(
            agentc._native,
            "optimize_observe_multi_call",
            create=True,
        ) as native,
    ):
        assert agentc.parallel_map(fn, [0, 1], on_observation=observations.append) == [
            0,
            1,
        ]
    native.assert_not_called()
    assert observations[0].status == "abstained"
    assert observations[0].cost_usd is None
    assert observations[0].execution_plan_id is None


def test_failed_callback_preserves_exception_and_accounts_only_completed_calls():
    failure = RuntimeError("original failure")
    observations = []

    def fn(item):
        record_call()
        if item == 0:
            raise failure
        return item

    with (
        agentc.application_version("release-v1"),
        patch.object(
            agentc._native,
            "optimize_observe_multi_call",
            create=True,
        ) as native,
        pytest.raises(RuntimeError) as caught,
    ):
        agentc.parallel_map(fn, [0, 1], on_observation=observations.append)
    assert caught.value is failure
    native.assert_not_called()
    assert observations[0].reason == "callback_failed"
    assert observations[0].sdk_calls == observations[0].observed_calls == 2
    assert get_parallel_peer() is None


def test_observation_callback_failure_preserves_result_and_original_exception():
    def broken_observer(_):
        raise SystemExit("observer cannot replace application behavior")

    assert agentc.parallel_map(lambda x: x, [1, 2], on_observation=broken_observer) == [
        1,
        2,
    ]
    failure = KeyboardInterrupt("original fatal signal")

    def fn(_):
        raise failure

    with pytest.raises(KeyboardInterrupt) as caught:
        agentc.parallel_map(fn, [1], on_observation=broken_observer)
    assert caught.value is failure


def test_nested_maps_restore_peer_and_abstain_for_outer_accounting():
    observations, inner_observations = [], []

    def fn(item):
        before = get_parallel_peer()
        assert agentc.parallel_map(
            lambda x: record_call() and x,
            [item],
            on_observation=inner_observations.append,
        ) == [item]
        assert get_parallel_peer() is before
        return item

    with (
        agentc.application_version("release-v1"),
        patch.object(
            agentc._native,
            "optimize_observe_multi_call",
            side_effect=native_record,
            create=True,
        ) as native,
    ):
        assert agentc.parallel_map(fn, [1, 2], on_observation=observations.append) == [
            1,
            2,
        ]
    assert native.call_count == 2
    assert all(value.status == "recorded" for value in inner_observations)
    assert observations[0].reason == "nested_map"


def test_application_version_required_and_scope_does_not_leak():
    observations = []
    with patch.object(
        agentc._native, "optimize_observe_multi_call", create=True
    ) as native:
        agentc.parallel_map(
            lambda x: record_call() and x, [1, 2], on_observation=observations.append
        )
    native.assert_not_called()
    assert observations[0].reason == "application_version_required"
    note_sdk_call()
    note_plan(plan())
    assert get_parallel_peer() is None


def test_native_failure_abstains_without_changing_map_response():
    observations = []
    with (
        agentc.application_version("release-v1"),
        patch.object(
            agentc._native,
            "optimize_observe_multi_call",
            side_effect=ValueError("native"),
            create=True,
        ),
    ):
        assert agentc.parallel_map(
            lambda x: record_call() and x, [1], on_observation=observations.append
        ) == [1]
    assert observations[0].reason == "native_observation_unavailable"


@pytest.mark.parametrize(
    "usage,source",
    [
        (
            {"prompt_tokens": 2, "completion_tokens": 1, "cost": 0.0, "is_byok": False},
            "provider_reported",
        ),
        (
            {
                "prompt_tokens": 2,
                "completion_tokens": 1,
                "cost": 0.01,
                "is_byok": False,
            },
            "provider_reported",
        ),
        (
            {"prompt_tokens": 2, "completion_tokens": 1, "cost": 0.01, "is_byok": True},
            "estimated",
        ),
        (
            {
                "prompt_tokens": 2,
                "completion_tokens": 1,
                "cost": float("nan"),
                "is_byok": False,
            },
            "estimated",
        ),
        (
            {
                "prompt_tokens": 2,
                "completion_tokens": 1,
                "cost": True,
                "is_byok": False,
            },
            "estimated",
        ),
        ({"prompt_tokens": 2, "cost": 0.01, "is_byok": False}, "estimated"),
        (
            {
                "prompt_tokens": True,
                "completion_tokens": 1,
                "cost": 0.01,
                "is_byok": False,
            },
            "estimated",
        ),
        (None, "unknown"),
    ],
)
def test_billing_source_does_not_promote_estimates_or_missing_usage(usage, source):
    from agentc._patches._optimizer_glue import build_outcome_openai

    response = SimpleNamespace(
        usage=None if usage is None else SimpleNamespace(**usage), choices=[]
    )
    measured = build_outcome_openai(
        response, elapsed_s=0.01, model="gpt-4o", call_site_id="fixture"
    )
    assert measured["cost_source"] == source


def test_comparison_fingerprints_are_ordered_and_reject_opaque_or_ambiguous_values():
    from agentc._batch_observation import comparison_fingerprint

    assert comparison_fingerprint([{"a": 1, "b": 2}]) == comparison_fingerprint(
        [{"b": 2, "a": 1}]
    )
    assert comparison_fingerprint(["a", "b"]) != comparison_fingerprint(["b", "a"])
    assert comparison_fingerprint([1]) != comparison_fingerprint([True])
    cyclic = []
    cyclic.append(cyclic)
    for value in [
        object(),
        {1: "not a string key"},
        float("nan"),
        float("inf"),
        cyclic,
        (1, 2),
    ]:
        assert comparison_fingerprint(value) is None


def test_comparison_contract_binds_original_inputs_and_final_results():
    from agentc._batch_observation import COMPARISON_VERSION, comparison_fingerprint

    captured, observations = [], []
    items = ["first", "second"]

    def fn(item):
        record_call()
        return {"result": item.upper()}

    def native(receipt):
        captured.append(json.loads(receipt))
        value = json.loads(native_record(receipt))
        value["comparison_token"] = "c" * 64
        return json.dumps(value)

    with (
        agentc.application_version("release-v1"),
        patch.object(
            agentc._native,
            "optimize_observe_multi_call",
            side_effect=native,
            create=True,
        ),
    ):
        assert agentc.parallel_map(
            fn, items, comparison=True, on_observation=observations.append
        ) == [{"result": "FIRST"}, {"result": "SECOND"}]
    assert captured[0]["comparison"] == {
        "version": COMPARISON_VERSION,
        "input_sha256": comparison_fingerprint(items),
        "output_sha256": comparison_fingerprint(
            [{"result": "FIRST"}, {"result": "SECOND"}]
        ),
    }
    assert observations[0].comparison_token == "c" * 64
    assert "cccccccc" not in repr(observations[0])


def test_unsupported_results_preserve_economics_without_a_comparison_receipt():
    captured, observations = [], []
    response = object()

    def native(receipt):
        captured.append(json.loads(receipt))
        return native_record(receipt)

    with (
        agentc.application_version("release-v1"),
        patch.object(
            agentc._native,
            "optimize_observe_multi_call",
            side_effect=native,
            create=True,
        ),
    ):
        assert agentc.parallel_map(
            lambda _: record_call() and response,
            [1],
            comparison=True,
            on_observation=observations.append,
        ) == [response]
    assert captured[0]["comparison"] is None
    assert observations[0].status == "recorded"
    assert observations[0].reason == "comparison_results_unsupported"
    assert observations[0].comparison_token is None


def test_comparison_api_requires_receipts_and_does_not_execute_any_callback():
    from agentc._batch_observation import ParallelObservation

    observation = ParallelObservation(
        status="recorded",
        reason=None,
        sdk_calls=1,
        observed_calls=1,
        wall_latency_ms=1.0,
    )
    with patch.object(
        agentc._native, "optimize_compare_multi_call", create=True
    ) as native:
        result = agentc.compare_parallel_observations(observation, observation)
    assert result.reason == "comparison_receipts_required"
    native.assert_not_called()


@pytest.mark.parametrize(
    "adapter,async_path",
    [
        ("openai", False),
        ("openai", True),
        ("anthropic", False),
        ("anthropic", True),
        ("litellm", False),
        ("litellm", True),
    ],
)
def test_real_adapter_bypass_is_counted_and_cannot_be_missing_from_batch(
    adapter, async_path
):
    import asyncio
    import importlib

    module = importlib.import_module(f"agentc._patches._{adapter}")
    name = (
        ("_wrap_acompletion" if async_path else "_wrap_completion")
        if adapter == "litellm"
        else ("_wrap_create_async" if async_path else "_wrap_create")
    )
    observations = []
    response = object()

    def wrapped(**_):
        return response

    async def wrapped_async(**kwargs):
        return wrapped(**kwargs)

    def fn(_):
        record_call()
        wrapper = getattr(module, name)
        if async_path:
            return asyncio.run(wrapper(wrapped_async, None, (), {}))
        return wrapper(wrapped, None, (), {})

    with (
        agentc.application_version("release-v1"),
        patch.object(module, "_is_initialized", return_value=False),
        patch.object(
            agentc._native,
            "optimize_observe_multi_call",
            create=True,
        ) as native,
    ):
        assert agentc.parallel_map(fn, [1, 2], on_observation=observations.append) == [
            response,
            response,
        ]
    native.assert_not_called()
    assert observations[0].reason == "unprofiled_sdk_call"
    assert observations[0].sdk_calls == 4
    assert observations[0].observed_calls == 2
