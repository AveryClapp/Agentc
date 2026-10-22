"""Actual cache payloads, SDK worker comparisons and native guarded admission."""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import agentc
from agentc import _native
from agentc._optimizer import observe_outcome, plan_call
from agentc._patches._optimizer_glue import (
    build_call_dict_anthropic,
    build_call_dict_openai,
    build_outcome_anthropic,
    build_outcome_openai,
    decode_cached_response,
    maybe_explore_record,
    maybe_explore_record_async,
)
from agentc._response_cache import seed_response
from agentc._writer import flush_blocking

PROTOCOLS = [
    "openai.chat.completions.v1",
    "anthropic.messages.v1",
    "litellm.completion.v1",
]
MODELS = ["gpt-4o-2024-11-20", "claude-sonnet-4-5-20250929", "openai/gpt-4o-2024-11-20"]


def response(model, text="cached answer"):
    return SimpleNamespace(
        model=model,
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        content=[SimpleNamespace(text=text)],
        usage=SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=300,
            input_tokens=100,
            output_tokens=300,
            cost=0.005,
            is_byok=False,
        ),
    )


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    agentc.shutdown()
    _native.optimize_reset()
    for key, value in {
        "AGENTC_OPTIMIZE": "1",
        "AGENTC_COMPOSE": "1",
        "AGENTC_MODEL_CATALOG_OFFLINE": "1",
        "AGENTC_ENABLED_RULES": "CacheHit",
        "AGENTC_EVAL_PLANNER_MODE": "joint_guarded",
        "AGENTC_OPTIMIZE_EXPLORATION": "1",
        "AGENTC_OPTIMIZE_SHADOW": "0.02",
        "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": "1000",
        "AGENTC_SHADOW_DIVERGENCE_MODE": "lexical",
        "AGENTC_STORAGE_PATH": str(tmp_path),
    }.items():
        monkeypatch.setenv(key, value)
    with patch("agentc._lifecycle._apply_patches"):
        agentc.init(storage_path=str(tmp_path), capture_embeddings=False)
    try:
        yield tmp_path
    finally:
        agentc.shutdown()
        _native.optimize_reset()


def restart(storage):
    agentc.shutdown()
    _native.merge_all_pending()
    with patch("agentc._lifecycle._apply_patches"):
        agentc.init(storage_path=str(storage), capture_embeddings=False)


def seeded_call(storage, protocol, *, tool_history=False):
    model = MODELS[PROTOCOLS.index(protocol)]
    kwargs = {
        "model": model,
        "messages": [{"role": "user", "content": "repeated task"}],
        "temperature": 0,
        "max_tokens": 1024,
    }
    if tool_history:
        kwargs["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "description": "Read a fixture",
                    "parameters": {
                        "type": "object",
                        "properties": {"id": {"type": "integer"}},
                        "required": ["id"],
                    },
                },
            }
        ]
        kwargs["messages"].extend(
            [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-original",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": '{"id":1}'},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call-original", "content": "result"},
                {"role": "user", "content": "Continue"},
            ]
        )
    builder = (
        build_call_dict_anthropic
        if protocol == PROTOCOLS[1]
        else build_call_dict_openai
    )
    options = {} if protocol == PROTOCOLS[1] else {"provider_protocol": protocol}
    call = builder(
        kwargs,
        call_site_id="cache.paired.admission",
        trace_id_hex="00" * 16,
        span_id_hex="01" * 8,
        **options,
    )
    outcome_builder = (
        build_outcome_anthropic if protocol == PROTOCOLS[1] else build_outcome_openai
    )
    cached_response = response(model)
    for _ in range(3):
        baseline = plan_call(call)
        assert baseline.kind == "pass_through"
        outcome = outcome_builder(
            cached_response,
            elapsed_s=0.1,
            model=model,
            call_site_id=call["call_site_id"],
            plan=baseline,
        )
        assert observe_outcome(baseline, outcome)
    seed_response(baseline, cached_response)
    assert flush_blocking()
    restart(storage)
    return call, outcome_builder


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("async_path", [False, True])
async def test_cached_plan_admits_only_after_twenty_real_sdk_comparisons_and_restart(
    runtime, protocol, async_path
):
    call, builder = seeded_call(runtime, protocol)
    physical_references = []
    lease_tokens = []
    candidate_ids = []

    def mutated(_call):
        pytest.fail("cache comparison must not issue another provider call")

    async def mutated_async(_call):
        mutated(_call)

    def extract(candidate, cached, elapsed):
        return builder(
            cached,
            elapsed_s=elapsed,
            model=cached.model,
            call_site_id=call["call_site_id"],
            plan=candidate,
        )

    for index in range(20):
        if index == 10:
            restart(runtime)
        plan = plan_call(call)
        assert plan.kind == "pass_through", (
            "unadmitted cache must preserve the reference"
        )
        assert plan.counterfactual is not None, (
            "cache candidate must receive a durable comparison lease"
        )
        assert plan.counterfactual.kind == "cached"
        candidate_data = json.loads(plan.counterfactual.raw_json)
        candidate_ids.append(
            candidate_data["agentc_observation_context"]["key"]["execution_plan_id"]
        )
        lease_tokens.append(plan.exploration_lease_token)
        # A fresh reference response is acquired for each admitted comparison.
        # Provider traffic is a local fixture; it never reaches a remote service.
        reference = response(call["model"])
        physical_references.append(reference)
        original_id = id(reference)
        outcome = builder(
            reference,
            elapsed_s=0.1,
            model=reference.model,
            call_site_id=call["call_site_id"],
            plan=plan,
        )
        assert outcome["cost_usd"] > 0 and observe_outcome(plan, outcome)
        if async_path:
            worker = maybe_explore_record_async(plan, reference, mutated_async, extract)
            assert worker is not None
            await worker
        else:
            worker = maybe_explore_record(plan, reference, mutated, extract)
            assert worker is not None
            worker.join(timeout=5)
            assert not worker.is_alive()
        assert id(reference) == original_id
        assert plan.exploration_lease_token is None, (
            "successful worker must commit its leased pair"
        )
    assert len(physical_references) == 20
    assert len(set(lease_tokens)) == 20
    assert len(set(candidate_ids)) == 1
    restart(runtime)
    admitted = plan_call(call)
    assert admitted.kind == "cached"
    assert admitted.counterfactual is None
    assert (
        decode_cached_response(admitted.value, provider_protocol=protocol).model
        == call["model"]
    )
    candidate_outcome = builder(
        response(call["model"]),
        elapsed_s=0.001,
        model=call["model"],
        call_site_id=call["call_site_id"],
        plan=admitted,
    )
    assert (
        candidate_outcome["input_tokens"],
        candidate_outcome["output_tokens"],
        candidate_outcome["cost_usd"],
    ) == (0, 0, 0.0)
    assert observe_outcome(admitted, candidate_outcome)
    state = inspect_state(runtime)
    assert state["leases"] == [["completed", 20, 0.0]]
    cached_rows = [row for row in state["profiles"] if row[0] == candidate_ids[0]]
    assert cached_rows[0][1:] == [20, 0.0, 0.0, 0.0]
    reference_rows = [row for row in state["profiles"] if row[0] != candidate_ids[0]]
    assert reference_rows and all(row[4] > 0 for row in reference_rows)


def inspect_state(storage):
    # Bundled rusqlite owns the live capture/control connections. Inspect their
    # persisted rows from an independent SQLite process, never this interpreter.
    import subprocess
    import sys

    _native.optimize_flush()
    script = """import json,sqlite3,sys
c=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True)
print(json.dumps({'leases':c.execute('SELECT status,COUNT(*),COALESCE(SUM(cost_usd),0) FROM execution_plan_exploration GROUP BY status').fetchall(),'profiles':c.execute('SELECT execution_plan_id,paired_observations,input_tokens_mean,output_tokens_mean,cost_usd_mean FROM execution_plan_profile').fetchall()}))
"""
    process = subprocess.run(
        [sys.executable, "-c", script, str(storage / "cost_model.db")],
        capture_output=True,
        text=True,
    )
    assert process.returncode == 0, process.stderr
    return json.loads(process.stdout)


def inspect_validation_state(storage):
    import subprocess
    import sys

    _native.optimize_flush()
    script = """import json,sqlite3,sys
c=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True)
print(json.dumps({'rejections':c.execute('SELECT COUNT(*) FROM execution_plan_validation_rejection').fetchone()[0], 'disabled':c.execute('SELECT reason,exposure FROM execution_plan_disabled').fetchall(), 'divergence_events':c.execute('SELECT COUNT(*) FROM execution_plan_guard_observation').fetchone()[0], 'charged_references':c.execute('SELECT COUNT(*),SUM(charged_reference_cost_usd),SUM(charged_reference_latency_ms) FROM execution_plan_observation WHERE charged_reference_cost_usd > 0').fetchone()}))
"""
    process = subprocess.run([sys.executable, "-c", script, str(storage / "cost_model.db")],
                             capture_output=True, text=True)
    assert process.returncode == 0, process.stderr
    return json.loads(process.stdout)


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("async_path", [False, True])
@pytest.mark.parametrize("accepted", [False, True])
async def test_native_cache_validation_charges_each_real_pair_and_survives_restart(
    runtime, protocol, async_path, accepted
):
    from agentc._output_validation import (
        ValidationContract, ValidationReceipt, bind_validation_request,
        experimental_validation_scope,
    )
    from agentc._patches._optimizer_glue import dispatch_sync

    call, builder = seeded_call(runtime, protocol)
    kwargs = {"model": call["model"], "messages": [{"role": "user", "content": "repeated task"}],
              "temperature": 0, "max_tokens": 1024}
    calls = []

    def validator(request):
        calls.append(request.execution_id)
        assert request.candidate_text == "cached answer"
        return ValidationReceipt(request.execution_id, request.contract_sha256,
                                 "fixture-pinned-v1", accepted, 7, 3, 0.0001, cost_source="provider_reported")

    contract = ValidationContract("cache-verifier-v1", "a" * 64, "fixture-pinned-v1",
                                  0.001, validator)
    with experimental_validation_scope(contract):
        call_builder = build_call_dict_anthropic if protocol == PROTOCOLS[1] else build_call_dict_openai
        options = {} if protocol == PROTOCOLS[1] else {"provider_protocol": protocol}
        call = call_builder(kwargs, call_site_id=call["call_site_id"], trace_id_hex="00" * 16,
                            span_id_hex="01" * 8, **options)

        def extract(candidate, cached, elapsed):
            return builder(cached, elapsed_s=elapsed, model=cached.model,
                           call_site_id=call["call_site_id"], plan=candidate)

        pair_count = 20 if accepted else 2
        for index in range(pair_count):
            if index == 10:
                restart(runtime)
            primary = plan_call(call)
            assert primary.kind == "pass_through" and primary.counterfactual.kind == "cached"
            bind_validation_request(primary, kwargs)
            candidate_id = json.loads(primary.counterfactual.raw_json)["agentc_observation_context"]["key"]["execution_plan_id"]
            reference = response(call["model"])
            assert observe_outcome(primary, builder(reference, elapsed_s=0.1, model=reference.model,
                                                   call_site_id=call["call_site_id"], plan=primary))
            if async_path:
                worker = maybe_explore_record_async(primary, reference, None, extract)
                assert worker is not None
                await worker
            else:
                worker = maybe_explore_record(primary, reference, None, extract)
                assert worker is not None
                worker.join(timeout=5)
                assert not worker.is_alive()
            assert primary.exploration_lease_token is None
        assert len(calls) == len(set(calls)) == pair_count
        state = inspect_state(runtime)
        assert state["leases"][0][:2] == ["completed", pair_count]
        assert state["leases"][0][2] == pytest.approx(pair_count * 0.0001)
        row = [row for row in state["profiles"] if row[0] == candidate_id][0]
        assert row[1:4] == [pair_count, 7.0, 3.0]
        assert row[4] == pytest.approx(0.0001)
        restart(runtime)
        selected = plan_call(call)
        if accepted:
            assert selected.kind == "cached"
            bind_validation_request(selected, kwargs)
            result = dispatch_sync(selected, run_original=lambda: pytest.fail("accepted cache must not dispatch"),
                                   run_mutated=None, decode_cached=lambda value: decode_cached_response(value, provider_protocol=protocol))
            measured = builder(result, elapsed_s=0.01, model=result.model,
                               call_site_id=call["call_site_id"], plan=selected)
            assert measured["cost_usd"] == 0.0
            assert measured["additional_inference"]["cost_usd"] == 0.0001
            assert observe_outcome(selected, measured)
            assert len(calls) == 21
            state = inspect_state(runtime)
            row = [row for row in state["profiles"] if row[0] == candidate_id][0]
            # A validator receipt itself did not add a 21st reference pair.
            assert row[1] == 20
            assert row[2:4] == [7.0, 3.0]
        else:
            assert selected.kind == "pass_through"
            assert selected.counterfactual is None
            validation_state = inspect_validation_state(runtime)
            assert validation_state["rejections"] == 2
            assert validation_state["disabled"] == [["validation_rejections", 0.0]]
            assert validation_state["divergence_events"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("vendor", ["openai", "anthropic", "litellm"])
@pytest.mark.parametrize("async_path", [False, True])
async def test_admitted_cache_drift_rejections_block_the_plan_across_restart(
    runtime, monkeypatch, vendor, async_path
):
    import importlib
    from agentc._output_validation import (
        ValidationContract, ValidationReceipt, bind_validation_request,
        experimental_validation_scope,
    )

    protocol = PROTOCOLS[["openai", "anthropic", "litellm"].index(vendor)]
    call, builder = seeded_call(runtime, protocol)
    kwargs = {"model": call["model"], "messages": [{"role": "user", "content": "repeated task"}],
              "temperature": 0, "max_tokens": 1024}
    drift = {"accepted": True}

    def validate(request):
        return ValidationReceipt(request.execution_id, request.contract_sha256,
                                 "drift-pinned-v1", drift["accepted"], 7, 3, 0.0001, cost_source="provider_reported")

    contract = ValidationContract("drift-control-v1", "a" * 64, "drift-pinned-v1", 0.001, validate)
    with experimental_validation_scope(contract):
        build = build_call_dict_anthropic if protocol == PROTOCOLS[1] else build_call_dict_openai
        options = {} if protocol == PROTOCOLS[1] else {"provider_protocol": protocol}
        call = build(kwargs, call_site_id=call["call_site_id"], trace_id_hex="00" * 16,
                     span_id_hex="01" * 8, **options)

        def extract(candidate, cached, elapsed):
            return builder(cached, elapsed_s=elapsed, model=cached.model,
                           call_site_id=call["call_site_id"], plan=candidate)

        for _ in range(20):
            primary = plan_call(call)
            assert primary.counterfactual is not None
            bind_validation_request(primary, kwargs)
            reference = response(call["model"])
            assert observe_outcome(primary, builder(reference, elapsed_s=0.1, model=reference.model,
                                                   call_site_id=call["call_site_id"], plan=primary))
            worker = maybe_explore_record(primary, reference, None, extract)
            assert worker is not None
            worker.join(5)
            assert not worker.is_alive()
        assert plan_call(call).kind == "cached"
        drift["accepted"] = False
        module = importlib.import_module("agentc._patches._" + vendor)
        planner, sync_name, async_name = {
            "openai": ("_plan_openai_call", "_wrap_create", "_wrap_create_async"),
            "anthropic": ("_plan_anthropic_call", "_wrap_create", "_wrap_create_async"),
            "litellm": ("_plan_call", "_wrap_completion", "_wrap_acompletion"),
        }[vendor]
        selected = []

        def plan(*_args):
            value = plan_call(call)
            selected.append(value)
            return value, call["call_site_id"]

        monkeypatch.setattr(module, planner, plan)
        monkeypatch.setattr(module, "_write_root_span", lambda *_args, **_kwargs: None)
        physical = []

        def original(*_args, **_kwargs):
            value = response(call["model"])
            physical.append(value)
            return value

        async def original_async(*args, **kwargs):
            return original(*args, **kwargs)

        wrapper = getattr(module, async_name if async_path else sync_name)
        for index in range(2):
            with patch.object(module, "_emit_span"):
                returned = (await wrapper(original_async, None, (), kwargs) if async_path else
                            wrapper(original, None, (), kwargs))
            assert returned is physical[-1]
            assert selected[-1].kind == "cached"
            assert selected[-1].validation_status == "rejected"
            assert len(physical) == index + 1
            restart(runtime)
        blocked = plan_call(call)
        assert blocked.kind == "pass_through" and blocked.counterfactual is None
        state = inspect_validation_state(runtime)
        assert state["rejections"] == 2
        assert state["disabled"] == [["validation_rejections", 0.0]]
        assert state["divergence_events"] == 0
        assert state["charged_references"][0] == 2
        assert state["charged_references"][1] > 0
        assert state["charged_references"][2] >= 0
        paired = inspect_state(runtime)["profiles"]
        assert sum(row[1] for row in paired) == 20


@pytest.mark.asyncio
@pytest.mark.parametrize("async_path", [False, True])
@pytest.mark.parametrize("failure", ["missing", "corrupt", "protocol", "mismatch"])
async def test_failed_or_harmful_cache_comparisons_do_not_admit_and_keep_reference(
    runtime, async_path, failure
):
    from agentc._response_cache import VERSION
    import pickle

    call, builder = seeded_call(runtime, PROTOCOLS[0])
    plan = plan_call(call)
    assert plan.counterfactual is not None
    reference = response(call["model"])
    outcome = builder(
        reference,
        elapsed_s=0.1,
        model=reference.model,
        call_site_id=call["call_site_id"],
        plan=plan,
    )
    assert observe_outcome(plan, outcome)
    candidate_id = json.loads(plan.counterfactual.raw_json)[
        "agentc_observation_context"
    ]["key"]["execution_plan_id"]
    if failure == "missing":
        raw = None
    elif failure == "corrupt":
        raw = b"corrupt pickle"
    else:
        raw = pickle.dumps(
            {
                "version": VERSION,
                "provider_protocol": PROTOCOLS[1]
                if failure == "protocol"
                else PROTOCOLS[0],
                "provider_namespace": "openai",
                "response": response(call["model"], "malicious different result"),
            }
        )

    def mutated(_call):
        pytest.fail("failed decode must never retry a provider request")

    async def mutated_async(_call):
        mutated(_call)

    def extract(candidate, cached, elapsed):
        return builder(
            cached,
            elapsed_s=elapsed,
            model=cached.model,
            call_site_id=call["call_site_id"],
            plan=candidate,
        )

    with patch("agentc._native.output_content_load", return_value=raw):
        if async_path:
            worker = maybe_explore_record_async(plan, reference, mutated_async, extract)
            assert worker is not None
            await worker
        else:
            worker = maybe_explore_record(plan, reference, mutated, extract)
            assert worker is not None
            worker.join(timeout=5)
            assert not worker.is_alive()
    assert reference.choices[0].message.content == "cached answer"
    assert plan.exploration_lease_token is None
    restart(runtime)
    next_plan = plan_call(call)
    assert next_plan.kind == "pass_through"
    state = inspect_state(runtime)
    by_status = {status: (count, cost) for status, count, cost in state["leases"]}
    if failure == "mismatch":
        assert by_status["completed"] == (1, 0)
        cached = [row for row in state["profiles"] if row[0] == candidate_id]
        assert cached[0][1:] == [1, 0.0, 0.0, 0.0]
    else:
        assert by_status["failed"] == (1, 0)
        assert not [row for row in state["profiles"] if row[0] == candidate_id]


@pytest.mark.asyncio
async def test_async_cache_decode_does_not_block_loop_and_cancellation_closes_lease(
    runtime,
):
    import asyncio
    import threading

    call, builder = seeded_call(runtime, PROTOCOLS[0])
    plan = plan_call(call)
    assert plan.counterfactual is not None
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def decode(*_args, **_kwargs):
        entered.set()
        try:
            assert release.wait(timeout=5)
            return response(call["model"])
        finally:
            finished.set()

    async def mutated(_call):
        pytest.fail("cached counterfactual must not call provider")

    def extract(*_args):
        pytest.fail("canceled comparison must not complete")

    with patch(
        "agentc._patches._optimizer_glue.decode_cached_response", side_effect=decode
    ):
        task = maybe_explore_record_async(
            plan, response(call["model"]), mutated, extract
        )
        assert task is not None
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            # The event loop remains responsive while a worker owns the decode.
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
        finally:
            release.set()
            assert await asyncio.to_thread(finished.wait, 2)
    assert plan.exploration_lease_token is None
    assert inspect_state(runtime)["leases"] == [["failed", 1, 0.0]]


def test_cached_fallback_cannot_complete_a_pair_and_replay_is_idempotent(runtime):
    from agentc._optimizer import complete_exploration, fail_exploration

    call, builder = seeded_call(runtime, PROTOCOLS[0])
    plan = plan_call(call)
    assert plan.counterfactual is not None
    candidate = decode_cached_response(
        plan.counterfactual.value, provider_protocol=PROTOCOLS[0]
    )
    outcome = builder(
        candidate,
        elapsed_s=0.001,
        model=candidate.model,
        call_site_id=call["call_site_id"],
        plan=plan.counterfactual,
    )
    outcome["dispatch_fallback"] = True
    assert not complete_exploration(plan, outcome, 0.0)
    assert plan.exploration_lease_token is not None
    assert fail_exploration(plan)
    assert inspect_state(runtime)["leases"] == [["failed", 1, 0.0]]
    next_plan = plan_call(call)
    assert next_plan.counterfactual is not None
    raw_token = next_plan.exploration_lease_token
    outcome.pop("dispatch_fallback")
    # Native code must ignore usage inherited from the stored SDK response.
    outcome.update(input_tokens=100, output_tokens=300, cost_usd=100.0)
    assert _native.optimize_complete_exploration(raw_token, json.dumps(outcome), 0.0)
    assert _native.optimize_complete_exploration(raw_token, json.dumps(outcome), 0.0)
    state = inspect_state(runtime)
    assert sorted(state["leases"]) == [["completed", 1, 0.0], ["failed", 1, 0.0]]
    candidate_id = json.loads(next_plan.counterfactual.raw_json)[
        "agentc_observation_context"
    ]["key"]["execution_plan_id"]
    assert [row for row in state["profiles"] if row[0] == candidate_id][0][1:] == [
        1,
        0.0,
        0.0,
        0.0,
    ]
    next_plan.exploration_lease_token = None


@pytest.mark.asyncio
@pytest.mark.parametrize("vendor", ["openai", "anthropic", "litellm"])
@pytest.mark.parametrize("async_path", [False, True])
async def test_actual_sdk_wrapper_preserves_reference_and_runs_cached_comparison(
    runtime, monkeypatch, vendor, async_path
):
    import importlib

    protocol = PROTOCOLS[["openai", "anthropic", "litellm"].index(vendor)]
    call, _builder = seeded_call(runtime, protocol)
    module = importlib.import_module("agentc._patches._" + vendor)
    names = {
        "openai": ("_plan_openai_call", "_wrap_create", "_wrap_create_async"),
        "anthropic": ("_plan_anthropic_call", "_wrap_create", "_wrap_create_async"),
        "litellm": ("_plan_call", "_wrap_completion", "_wrap_acompletion"),
    }
    planner, sync_name, async_name = names[vendor]
    planned = []

    def plan(*_args):
        value = plan_call(call)
        planned.append(value)
        return value, call["call_site_id"]

    monkeypatch.setattr(module, planner, plan)
    monkeypatch.setattr(module, "_write_root_span", lambda *_args, **_kwargs: None)
    actual = []

    def original(*_args, **_kwargs):
        value = response(call["model"])
        actual.append(value)
        return value

    async def original_async(*args, **kwargs):
        return original(*args, **kwargs)

    kwargs = {
        "model": call["model"],
        "messages": [{"role": "user", "content": "repeated task"}],
        "temperature": 0,
        "max_tokens": 1024,
    }
    with patch.object(module, "_emit_span"):
        wrapper = getattr(module, async_name if async_path else sync_name)
        returned = (
            await wrapper(original_async, None, (), kwargs)
            if async_path
            else wrapper(original, None, (), kwargs)
        )
    assert len(actual) == 1 and returned is actual[0]
    assert len(planned) == 1 and planned[0].counterfactual.kind == "cached"
    if async_path:
        from agentc._patches import _optimizer_glue as glue

        with glue._exploration_workers_lock:
            tasks = [
                task
                for task, value in glue._exploration_tasks.items()
                if value is planned[0]
            ]
        for task in tasks:
            await task
    else:
        from agentc._patches._optimizer_glue import drain_exploration

        drain_exploration(5000)
    assert planned[0].exploration_lease_token is None
    assert inspect_state(runtime)["leases"] == [["completed", 1, 0.0]]


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", [PROTOCOLS[0], PROTOCOLS[2]])
async def test_native_tool_history_cache_uses_original_shape_and_reaches_guarded_admission(
    runtime, protocol
):
    call, builder = seeded_call(runtime, protocol, tool_history=True)
    assert call["parameters"]["extra"]["agentc_native_messages_opaque"]

    def mutated(_call):
        pytest.fail("cache validation must not execute tools or call a provider")

    for _ in range(20):
        plan = plan_call(call)
        assert plan.kind == "pass_through" and plan.counterfactual is not None
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
        worker = maybe_explore_record(
            plan,
            reference,
            mutated,
            lambda candidate, cached, elapsed: builder(
                cached,
                elapsed_s=elapsed,
                model=cached.model,
                call_site_id=call["call_site_id"],
                plan=candidate,
            ),
        )
        assert worker is not None
        worker.join(timeout=5)
        assert not worker.is_alive() and plan.exploration_lease_token is None
    restart(runtime)
    assert plan_call(call).kind == "cached"
    assert inspect_state(runtime)["leases"] == [["completed", 20, 0.0]]
