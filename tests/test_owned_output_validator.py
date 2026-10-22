"""Owned validator budgeting, paid failure liabilities and cancellation on loopback."""

import asyncio
import json
import threading
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agentc._executor import dispatch
from agentc._optimizer import Plan
from agentc._output_validation import (
    bind_validation_request,
    experimental_validation_scope,
)
from agentc._patches._optimizer_glue import dispatch_span_attributes, dispatch_sync
from bench import campaign_accounting as accounting
from bench.campaign_transport import CampaignStopped
from bench.output_validator import OwnedOutputValidator
from test_campaign_transport import CONTRACT, metadata, provider as provider, transport


def candidate():
    return SimpleNamespace(
        model="gpt-4o-2024-11-20",
        choices=[SimpleNamespace(message=SimpleNamespace(content="candidate"))],
        usage=SimpleNamespace(
            prompt_tokens=10, completion_tokens=2, cost=0.005, is_byok=False
        ),
    )


def validator(client, **kwargs):
    return OwnedOutputValidator(
        transport=client,
        serving=CONTRACT,
        metadata=metadata(),
        max_cost_usd="0.002",
        output_tokens=8,
        **kwargs,
    )


def bind(plan):
    bind_validation_request(
        plan,
        {
            "model": "gpt-4o",
            "messages": [
                {
                    "role": "tool",
                    "tool_call_id": "original-tool-id",
                    "content": "tool result",
                }
            ],
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("async_path", [False, True])
@pytest.mark.parametrize(
    "verdict",
    [
        '{"accepted":true}',
        '{"accepted":false}',
        '{"accepted":"true"}',
        '{"accepted":true,"accepted":false}',
    ],
)
async def test_owned_validator_reserves_once_and_bills_unknown_verdicts(
    tmp_path, provider, async_path, verdict
):
    client = transport(tmp_path, provider)
    observed_at_post = []

    def reply(body):
        snapshot = accounting.read_snapshot(client.ledger)
        observed_at_post.append(snapshot.state["pending_usd"])
        body["choices"][0]["message"]["content"] = verdict
        return body

    provider.mutate = reply
    owned = validator(client)
    plan = Plan(kind="cached")
    value = candidate()
    references = []

    def original():
        references.append("reference")
        return "reference"

    async def original_async():
        return original()

    with experimental_validation_scope(owned.contract()):
        bind(plan)
        result = (
            await dispatch(
                plan,
                run_original=original_async,
                run_mutated=None,
                decode_cached=lambda _: value,
            )
            if async_path
            else dispatch_sync(
                plan,
                run_original=original,
                run_mutated=None,
                decode_cached=lambda _: value,
            )
        )
    accepted = verdict == '{"accepted":true}'
    assert result is value if accepted else result == "reference"
    assert references == ([] if accepted else ["reference"])
    assert len(provider.posts) == 1
    bound = accounting.money("0.001008")
    assert observed_at_post == [bound]
    snapshot = accounting.read_snapshot(client.ledger)
    assert snapshot.state["known_usd"] == accounting.money("0.0001")
    assert snapshot.state["holds_usd"] == snapshot.state["pending_usd"] == 0
    claim = next(e for e in snapshot.events if e["event"] == "reserve")
    assert claim["metadata"]["kind"] == "validation"
    assert "parent_native_plan_json" in claim["metadata"]
    transmitted = json.loads(provider.posts[0]["messages"][-1]["content"])
    assert (
        transmitted["original_request"]["messages"][0]["tool_call_id"]
        == "original-tool-id"
    )
    assert transmitted["candidate"] == "candidate"
    assert plan.additional_inference == {
        "input_tokens": 2,
        "output_tokens": 1,
        "cost_usd": 0.0001,
        "cost_source": "provider_reported",
    }
    client.assert_complete()


@pytest.mark.asyncio
@pytest.mark.parametrize("async_path", [False, True])
async def test_owned_validator_funding_failure_cannot_run_an_unreserved_retry(
    tmp_path, provider, async_path
):
    client = transport(tmp_path, provider, cap="0.0001")
    owned = validator(client)
    value = candidate()
    plan = Plan(kind="rewritten", call={"model": value.model})
    references = []

    def original():
        references.append("reference")

    async def original_async():
        original()

    async def mutated(_call):
        return value

    with experimental_validation_scope(owned.contract()):
        bind(plan)
        with pytest.raises(CampaignStopped):
            if async_path:
                await dispatch(plan, run_original=original_async, run_mutated=mutated)
            else:
                dispatch_sync(plan, run_original=original, run_mutated=lambda _: value)
    assert references == provider.posts == []
    assert plan.additional_inference["cost_usd"] == 0.005
    assert plan.validation_uncertain_budget_usd == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("async_path", [False, True])
async def test_provider_billing_failure_retains_known_charge_and_remainder(
    tmp_path, provider, async_path
):
    client = transport(tmp_path, provider)
    owned = validator(client)
    provider.status = 503
    plan = Plan(kind="cached")
    references = []

    async def original_async():
        references.append("reference")

    with experimental_validation_scope(owned.contract()):
        bind(plan)
        with pytest.raises(CampaignStopped):
            if async_path:
                await dispatch(
                    plan,
                    run_original=original_async,
                    run_mutated=None,
                    decode_cached=lambda _: candidate(),
                )
            else:
                dispatch_sync(
                    plan,
                    run_original=lambda: references.append("reference"),
                    run_mutated=None,
                    decode_cached=lambda _: candidate(),
                )
    assert references == []
    assert plan.additional_inference["cost_usd"] == 0.0001
    assert plan.validation_uncertain_budget_usd == pytest.approx(0.000908)
    assert not plan.inference_cost_complete
    attrs = dispatch_span_attributes(plan)
    assert attrs["agentc.validation.uncertain_budget_usd"] == pytest.approx(0.000908)
    snapshot = accounting.read_snapshot(client.ledger)
    assert snapshot.state["known_usd"] == accounting.money("0.0001")
    assert snapshot.state["holds_usd"] == accounting.money("0.000908")


@pytest.mark.asyncio
async def test_async_cancellation_closes_owned_worker_and_retains_full_reservation(
    tmp_path, provider
):
    client = transport(tmp_path, provider)
    provider.block_all = True
    owned = validator(client)
    plan = Plan(kind="cached")
    references = []

    async def original():
        references.append("reference")

    with experimental_validation_scope(owned.contract()):
        bind(plan)
        task = asyncio.create_task(
            dispatch(
                plan,
                run_original=original,
                run_mutated=None,
                decode_cached=lambda _: candidate(),
            )
        )
        assert await asyncio.to_thread(provider.background_started.wait, 4)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert references == []
    assert len(provider.posts) == 1
    assert not client._active
    assert plan.additional_inference["cost_usd"] == 0
    assert plan.validation_uncertain_budget_usd == pytest.approx(0.001008)
    snapshot = accounting.read_snapshot(client.ledger)
    assert snapshot.state["pending_usd"] == 0
    assert snapshot.state["holds_usd"] == accounting.money("0.001008")
    assert snapshot.state["failed_calls"]
    with pytest.raises(CampaignStopped):
        client.assert_complete()


def test_declared_cap_is_checked_before_any_provider_operation(tmp_path, provider):
    client = transport(tmp_path, provider)
    with pytest.raises(CampaignStopped, match="declared cost cap"):
        OwnedOutputValidator(
            transport=client,
            serving=CONTRACT,
            metadata=metadata(),
            max_cost_usd="0.0001",
            output_tokens=8,
        )
    assert provider.posts == provider.gets == []


def test_failure_receipt_reads_do_not_change_the_journal(tmp_path, provider):
    client = transport(tmp_path, provider)
    before = client.ledger.read_bytes()
    receipt = client.call_accounting("not-dispatched")
    assert receipt["complete"] and receipt["known_cost_usd"] == "0"
    assert client.ledger.read_bytes() == before


@pytest.mark.parametrize(
    "phase", ["before_preflight", "after_preflight", "after_reservation"]
)
def test_cancelled_transport_never_posts_and_preserves_any_issued_hold(
    tmp_path, provider, monkeypatch, phase
):
    from test_campaign_transport import request

    client = transport(tmp_path, provider)
    cancelled = threading.Event()
    if phase == "before_preflight":
        cancelled.set()
    elif phase == "after_preflight":
        preflight = client._preflight

        def complete_preflight(*args):
            result = preflight(*args)
            cancelled.set()
            return result

        monkeypatch.setattr(client, "_preflight", complete_preflight)
    else:
        append = client._append

        def append_then_cancel(stream, events, event):
            append(stream, events, event)
            if event["event"] == "reserve":
                cancelled.set()

        monkeypatch.setattr(client, "_append", append_then_cancel)
    with pytest.raises(CampaignStopped):
        client.call(
            call_id="cancelled",
            request=request(),
            metadata=metadata(kind="validation"),
            cancelled=cancelled,
        )
    assert provider.posts == []
    receipt = client.call_accounting("cancelled")
    assert receipt["status"] == (
        "failed" if phase == "after_reservation" else "not_dispatched"
    )
    assert accounting.money(receipt["uncertain_budget_usd"]) == accounting.money(
        "0.001008" if phase == "after_reservation" else "0"
    )


def test_validator_configuration_cannot_change_after_binding(tmp_path, provider):
    owned = validator(transport(tmp_path, provider))
    with pytest.raises(FrozenInstanceError):
        owned.output_tokens = 16
    with pytest.raises(TypeError):
        owned.metadata["task_id"] = "different-task"


@pytest.mark.asyncio
@pytest.mark.parametrize("vendor", ["openai", "anthropic", "litellm"])
@pytest.mark.parametrize("async_path", [False, True])
async def test_all_six_sdk_error_spans_keep_validator_failure_liabilities(
    tmp_path, provider, monkeypatch, vendor, async_path
):
    import importlib

    client = transport(tmp_path, provider)
    provider.status = 503
    owned = validator(client)
    plan = Plan(kind="cached")
    module = importlib.import_module("agentc._patches._" + vendor)
    planner, sync_name, async_name = {
        "openai": ("_plan_openai_call", "_wrap_create", "_wrap_create_async"),
        "anthropic": ("_plan_anthropic_call", "_wrap_create", "_wrap_create_async"),
        "litellm": ("_plan_call", "_wrap_completion", "_wrap_acompletion"),
    }[vendor]
    monkeypatch.setattr(module, "_is_initialized", lambda: True)
    monkeypatch.setattr(module, planner, lambda *_args: (plan, "owned-validator"))
    monkeypatch.setattr(module, "_write_root_span", lambda *_args, **_kwargs: None)
    references = []

    def original(*_args, **_kwargs):
        references.append("reference")

    async def original_async(*args, **kwargs):
        original(*args, **kwargs)

    with (
        experimental_validation_scope(owned.contract()),
        patch.object(module, "_emit_span") as emit,
        patch(
            "agentc._patches._optimizer_glue.decode_cached_response",
            return_value=candidate(),
        ),
    ):
        wrapper = getattr(module, async_name if async_path else sync_name)
        kwargs = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "request"}],
            "max_tokens": 8,
        }
        with pytest.raises(CampaignStopped):
            if async_path:
                await wrapper(original_async, None, (), kwargs)
            else:
                wrapper(original, None, (), kwargs)
    assert references == []
    emit.assert_called_once()
    attrs = emit.call_args.kwargs["attrs"]
    assert attrs["agentc.validation.additional_inference.cost_usd"] == 0.0001
    assert attrs["agentc.validation.uncertain_budget_usd"] == pytest.approx(0.000908)
    assert attrs["agentc.validation.cost_complete"] is False
