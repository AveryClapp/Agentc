"""Every real provider dispatch survives fallback, cancellation and late work."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys

import pytest


def test_receipt_failure_preserves_original_response_and_exception(monkeypatch):
    from agentc import _native
    from agentc._provider_receipts import (
        ProviderReceiptBatch,
        instrument_provider,
        note_logical_call,
    )

    def fail(_):
        raise RuntimeError("observer failed")

    monkeypatch.setattr(_native, "optimize_record_provider_receipt", fail)
    batch = ProviderReceiptBatch("a" * 64, "callback", 1)
    response = object()
    error = RuntimeError("original")
    with batch.item(0):
        note_logical_call(0)
        wrapped = instrument_provider(
            lambda **_: response, None, "openai.chat.completions.v1"
        )
        assert wrapped(model="model", messages=[]) is response

        def broken(**_):
            raise error

        wrapped = instrument_provider(broken, None, "openai.chat.completions.v1")
        with pytest.raises(RuntimeError) as caught:
            wrapped(model="model", messages=[])
        assert caught.value is error
    assert batch.failed and batch.seal([1]) is None


@pytest.mark.asyncio
async def test_async_receipt_keeps_cancellation_unknown_and_preserves_it(monkeypatch):
    from agentc import _native
    from agentc._provider_receipts import (
        ProviderReceiptBatch,
        instrument_provider,
        note_logical_call,
    )

    commands = []

    def record(value):
        commands.append(json.loads(value))
        return '{"status":"recorded"}'

    monkeypatch.setattr(_native, "optimize_record_provider_receipt", record)
    batch = ProviderReceiptBatch("a" * 64, "callback", 1)
    error = asyncio.CancelledError()

    async def cancelled(**_):
        raise error

    with batch.item(0):
        note_logical_call(0)
        wrapped = instrument_provider(
            cancelled, None, "openai.chat.completions.v1", asynchronous=True
        )
        with pytest.raises(asyncio.CancelledError) as caught:
            await wrapped(model="model", messages=[])
        assert caught.value is error
    completion = commands[-1]["completion"]
    assert completion["status"] == "cancelled"
    assert completion["cost_usd"] is None and completion["http_attempts"] is None


def test_mutated_failure_marks_unknown_cost_without_a_validator():
    from agentc._optimizer import Plan
    from agentc._patches._optimizer_glue import dispatch_sync

    plan = Plan(kind="rewritten", call={"model": "model"})

    def fail(_):
        raise RuntimeError("failed dispatch")

    response = object()
    assert (
        dispatch_sync(plan, run_original=lambda: response, run_mutated=fail) is response
    )
    assert plan.inference_cost_complete is False


def test_real_sdk_journal_counts_fallback_validation_shadow_and_late_probe(tmp_path):
    script = r"""
import json,threading,contextvars
from dataclasses import asdict
import agentc,openai,httpx
from agentc._evaluation_policy import FrozenEvaluationPolicy,evaluation_policy_scope
from agentc._dispatch_context import provider_dispatch
from agentc._optimization_scope import optimization_scope
from agentc._provider_receipts import instrument_provider
calls=[]
fail=True
def handler(request):
    body=json.loads(request.content);calls.append(body)
    if fail and body["model"]=="gpt-4o-mini-2024-07-18":
        return httpx.Response(500,json={"error":{"message":"fixture","type":"server_error"}})
    return httpx.Response(200,json={"id":"fixture","object":"chat.completion","created":0,"model":body["model"],"choices":[{"index":0,"finish_reason":"stop","message":{"role":"assistant","content":"answer"}}],"usage":{"prompt_tokens":2,"completion_tokens":1,"total_tokens":3,"cost":0.01,"is_byok":False}})
client=openai.OpenAI(api_key="fixture",max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(handler)))
configuration={"strong_model":"gpt-4o-2024-11-20","cheap_model":"gpt-4o-mini-2024-07-18","divergence_threshold":0.05,"static_plan":{"target_model":"gpt-4o-mini-2024-07-18","rewrites":[{"rule":"OutputBudget","max_output_tokens":128}]},"router":None,"rewrite_by_target":{}}
policy=FrozenEvaluationPolicy.from_configuration(mode="best_static_joint",calibration_lock_sha256="b"*64,configuration=configuration)
observations=[]
agentc.init()
try:
    with agentc.application_version("physical-journal-v1"),evaluation_policy_scope(policy):
        responses=agentc.parallel_map(lambda _:client.chat.completions.create(model="gpt-4o-2024-11-20",messages=[{"role":"user","content":"private source prompt"}],max_tokens=1024),[0],max_workers=1,on_observation=observations.append)
    assert len(calls)==2 and responses[0].model=="gpt-4o-2024-11-20"
    fallback=observations[-1]
    assert fallback.provider_receipts["sdk_provider_invocations"]==2,fallback
    assert fallback.provider_receipts["known_provider_cost_usd"]==0.01
    assert fallback.provider_receipts["unknown_cost_invocations"]==1
    assert not fallback.provider_receipts["cost_complete"]
    # A fresh real issued original plan owns later auxiliary invocations.
    from agentc._batch_observation import BatchObservation,note_sdk_call,note_plan
    from agentc._optimizer import plan_call
    from agentc._patches._optimizer_glue import build_call_dict_openai
    batch=BatchObservation(application_version="a"*64,callback_version="auxiliary-fixture",item_count=1,max_workers=1)
    fail=False
    body={"model":"gpt-4o-2024-11-20","messages":[{"role":"user","content":"private auxiliary prompt"}],"max_tokens":8}
    with batch.item(0),agentc.application_version("auxiliary-v1"):
        note_sdk_call()
        source=build_call_dict_openai(body,call_site_id="auxiliary",trace_id_hex="0"*32,span_id_hex="0"*16)
        with optimization_scope("original",optimize=False):
            plan=plan_call(source)
        provider_call=instrument_provider(client.chat.completions.create.__wrapped__,client.chat.completions,"openai.chat.completions.v1")
        def invoke(role):
            with provider_dispatch(role,plan=plan,primary_plan=plan):
                return provider_call(**body)
        invoke("primary");invoke("validation");invoke("shadow")
        context=contextvars.copy_context()
    foreground=batch.provider_receipts.seal([1])
    assert foreground["sdk_provider_invocations"]==3,foreground
    context.run(invoke,"exploration")
    report=json.loads(agentc._native.optimize_record_provider_receipt(json.dumps({"operation":"report","batch_id":batch.provider_receipts.batch_id})))["summary"]
    assert report["sdk_provider_invocations"]==4,report
    assert report["known_http_attempts"]==4
    assert abs(report["known_provider_cost_usd"]-.04)<1e-12
    assert len(calls)==6
    print(json.dumps({"http_invocations":len(calls),"fallback":asdict(fallback),"auxiliary_batch_id":batch.provider_receipts.batch_id,"auxiliary":report}))
finally:
    agentc.shutdown();client.close()
"""
    proc = subprocess.run(
        [sys.executable, "-I", "-c", script],
        env={
            **os.environ,
            "AGENTC_STORAGE_PATH": str(tmp_path),
            "AGENTC_EVAL_PLANNER_MODE": "best_static_joint",
            "AGENTC_OPTIMIZE": "1",
            "AGENTC_OPTIMIZE_EXPLORATION": "0",
            "AGENTC_OPTIMIZE_SHADOW": "0",
            "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": "1000",
            "AGENTC_CAPTURE_CONTENT": "0",
            "AGENTC_CAPTURE_EMBEDDINGS": "0",
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["http_invocations"] == 6


def test_prepared_journal_cannot_be_reused_to_publish_another_execution(tmp_path):
    script = r"""
import json
import agentc,httpx,openai
from agentc._response_cache import request_declaration
calls=[]
def handler(request):
    calls.append(1)
    return httpx.Response(200,json={"id":"fixture","object":"chat.completion","created":0,"model":"gpt-4o-mini-2024-07-18","choices":[{"index":0,"finish_reason":"stop","message":{"role":"assistant","content":"answer"}}],"usage":{"prompt_tokens":2,"completion_tokens":1,"total_tokens":3,"cost":0.01,"is_byok":False}})
client=openai.OpenAI(api_key="fixture",max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(handler)))
captured=[]
observe=agentc._native.optimize_observe_multi_call
def capture(value):
    result=observe(value);captured.append((value,result));return result
agentc._native.optimize_observe_multi_call=capture
agentc.init()
try:
    with agentc.application_version("publication-v1"):
        body={"model":"gpt-4o-mini","messages":[{"role":"user","content":"private original"}],"max_tokens":8}
        output=[]
        agentc.completion_batch([agentc.CompletionRequest("openai",body,client)],site="publication",policy=agentc.BatchPolicy(1000),mode="reference",on_observation=output.append)
        assert output[0].status=="recorded",output
        raw,result=captured[0]
        assert observe(raw)==result
        edited=json.loads(raw);edited["wall_latency_ms"]+=1
        assert json.loads(observe(json.dumps(edited)))["status"]=="abstained"
        edited=json.loads(raw);source=json.loads(edited["calls"][0]["source_call_json"])
        source["messages"][0]["content"]="different input"
        source["parameters"]["extra"]["agentc_route_context"]["sdk_response_cache"]=request_declaration({**body,"messages":[{"role":"user","content":"different input"}]})
        edited["calls"][0]["source_call_json"]=json.dumps(source)
        assert json.loads(observe(json.dumps(edited)))["status"]=="abstained"
        assert len(calls)==1
    print(json.dumps({"http_invocations":len(calls),"batch_id":output[0].provider_receipt_batch_id}))
finally:
    agentc.shutdown();client.close()
"""
    proc = subprocess.run(
        [sys.executable, "-I", "-c", script],
        env={
            **os.environ,
            "AGENTC_STORAGE_PATH": str(tmp_path),
            "AGENTC_OPTIMIZE": "1",
            "AGENTC_OPTIMIZE_EXPLORATION": "0",
            "AGENTC_OPTIMIZE_SHADOW": "0",
            "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": "1000",
            "AGENTC_CAPTURE_CONTENT": "0",
            "AGENTC_CAPTURE_EMBEDDINGS": "0",
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["http_invocations"] == 1
