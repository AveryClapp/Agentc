"""Prepared SDK requests, native admission and exact provider-call boundaries."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest


def test_prepared_plan_is_single_use_bound_to_protocol_and_exact_payload():
    from agentc._optimizer import Plan
    from agentc._prepared_plan import consume_prepared_plan, prepared_plan

    plan = Plan(kind="pass_through")
    kwargs = {"model": "model", "messages": [{"role": "user", "content": "a"}]}
    with prepared_plan(plan, "protocol", kwargs, "site"):
        with pytest.raises(RuntimeError):
            consume_prepared_plan({**kwargs, "model": "other"}, "protocol")
        assert consume_prepared_plan(kwargs, "protocol") == (plan, "site")
        with pytest.raises(RuntimeError):
            consume_prepared_plan(kwargs, "protocol")
    assert consume_prepared_plan(kwargs, "protocol") is None
    with prepared_plan(plan, "protocol", kwargs, "site"):
        with pytest.raises(RuntimeError):
            consume_prepared_plan(kwargs, "other")
    client = object()
    with prepared_plan(plan, "protocol", kwargs, "site", dispatch_client=client):
        for supplied in ({}, {"client": object()}):
            with pytest.raises(RuntimeError):
                consume_prepared_plan({**kwargs, **supplied}, "protocol")
        assert consume_prepared_plan({**kwargs, "client": client}, "protocol") == (
            plan,
            "site",
        )


def test_prepared_client_validation_rejects_retries_hooks_opaque_and_stateful_requests():
    import httpx
    import openai
    from agentc._completion_batch import CompletionRequest, _freeze

    def client(**kwargs):
        return openai.OpenAI(
            api_key="fixture",
            http_client=httpx.Client(
                transport=httpx.MockTransport(lambda _: httpx.Response(500))
            ),
            **kwargs,
        )

    body = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "a"}]}
    retrying = client(max_retries=1)
    valid = client(max_retries=0)
    try:
        with pytest.raises(ValueError):
            _freeze(CompletionRequest("openai", body, retrying))
        for extra in [
            {"store": True},
            {"stream": True},
            {"success_callback": lambda _: None},
            {"temperature": object()},
            {"num_retries": 0},
        ]:
            with pytest.raises(ValueError):
                _freeze(CompletionRequest("openai", {**body, **extra}, valid))
        valid._client.event_hooks["request"].append(lambda _: None)
        with pytest.raises(ValueError):
            _freeze(CompletionRequest("openai", body, valid))
        with pytest.raises(ValueError):
            _freeze(CompletionRequest("openai", body, SimpleNamespace()))
    finally:
        retrying.close()
        valid.close()


def test_prepared_requests_are_copied_and_client_mutation_is_detected():
    import httpx
    import openai
    from agentc._completion_batch import CompletionRequest, _assert_unchanged, _freeze

    client = openai.OpenAI(
        api_key="fixture",
        max_retries=0,
        http_client=httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(500))
        ),
    )
    body = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "a"}]}
    try:
        frozen = _freeze(CompletionRequest("openai", body, client))
        body["messages"][0]["content"] = "changed"
        assert frozen.kwargs["messages"][0]["content"] == "a"
        assert frozen.kwargs["store"] is False
        assert "store" not in body
        _assert_unchanged(frozen)
        client.base_url = "https://changed.invalid/v1/"
        with pytest.raises(RuntimeError):
            _assert_unchanged(frozen)
    finally:
        client.close()


def test_prepared_litellm_rejects_implicit_clients_hooks_and_fallbacks(monkeypatch):
    import httpx
    import openai
    import agentc._completion_batch as batches

    def completion(**kwargs):
        return kwargs

    completion.__module__ = "litellm.main"
    litellm = SimpleNamespace(completion=completion)
    monkeypatch.setitem(sys.modules, "litellm", litellm)
    actual_version = batches.version
    monkeypatch.setattr(
        batches,
        "version",
        lambda name: "fixture" if name == "litellm" else actual_version(name),
    )
    body = {
        "model": "openai/gpt-4o-mini",
        "messages": [{"role": "user", "content": "a"}],
    }
    client = openai.OpenAI(
        api_key="fixture",
        max_retries=0,
        http_client=httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(500))
        ),
    )
    try:
        with pytest.raises(ValueError):
            batches._freeze(batches.CompletionRequest("litellm", body))
        request = batches.CompletionRequest("litellm", body, client)
        frozen = batches._freeze(request)
        assert "client" not in frozen.kwargs
        assert frozen.invoke(**frozen.kwargs)["client"] is client
        assert frozen.transport_version["backend"]["base_url"] == str(client.base_url)
        batches._assert_unchanged(frozen)
        for name in (
            "input_callback",
            "pre_call_rules",
            "model_fallbacks",
            "cache",
            "headers",
            "drop_params",
        ):
            setattr(litellm, name, [object()])
            with pytest.raises(ValueError):
                batches._freeze(request)
            with pytest.raises(ValueError):
                batches._assert_unchanged(frozen)
            setattr(litellm, name, None)
        client.base_url = "https://changed.invalid/v1/"
        with pytest.raises(RuntimeError):
            batches._assert_unchanged(frozen)
        with pytest.raises(ValueError):
            batches._freeze(
                batches.CompletionRequest(
                    "litellm", {**body, "model": "anthropic/claude"}, client
                )
            )
    finally:
        client.close()


def test_litellm_store_forwarding_control_has_its_own_supported_cohort():
    from agentc._provider_config import declaration

    protocol = "litellm.completion.v1"
    baseline = declaration({}, provider_protocol=protocol)
    prepared = declaration(
        {"allowed_openai_params": ["store"]}, provider_protocol=protocol
    )
    assert prepared["supported"]
    assert prepared["sha256"] != baseline["sha256"]
    for value in ([], ["audio"], ["store", "unknown"], "store", None):
        assert not declaration(
            {"allowed_openai_params": value}, provider_protocol=protocol
        )["supported"]
    assert not declaration(
        {"allowed_openai_params": ["store"]},
        provider_protocol="openai.chat.completions.v1",
    )["supported"]


@pytest.mark.parametrize(
    "fields,expected_source",
    [
        ({"cost": 0.5, "is_byok": False}, "provider_reported"),
        ({"cost": True, "is_byok": False}, "estimated"),
        ({"cost": float("nan"), "is_byok": False}, "estimated"),
        ({"cost": -1, "is_byok": False}, "estimated"),
        ({"cost": 0.5}, "estimated"),
        ({"cost": 0.5, "is_byok": True}, "estimated"),
        ({"cost": 0.5, "is_byok": False, "input_tokens": True}, "estimated"),
        ({"cost": 0.5, "is_byok": False, "output_tokens": None}, "estimated"),
    ],
)
def test_anthropic_reported_billing_requires_complete_tokens_and_known_payer(
    fields, expected_source
):
    from agentc._patches._optimizer_glue import build_outcome_anthropic

    response = SimpleNamespace(
        usage=SimpleNamespace(**{"input_tokens": 2, "output_tokens": 1, **fields}),
        content=[SimpleNamespace(text="answer")],
    )
    outcome = build_outcome_anthropic(
        response,
        elapsed_s=0.01,
        model="claude-haiku-4-5-20251001",
        call_site_id="fixture",
    )
    assert outcome["cost_source"] == expected_source
    if expected_source == "provider_reported":
        assert outcome["cost_usd"] == 0.5


def test_prepared_real_sdk_mixed_batch_keeps_one_http_call_per_item(tmp_path):
    pytest.importorskip("anthropic")
    pytest.importorskip("litellm")
    script = r"""
import os
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"]="True"
import json,threading
from dataclasses import asdict
import agentc,anthropic,httpx,litellm,openai
litellm.telemetry=False
calls,observations=[],[]
lock=threading.Lock()
def handler(request):
    body=json.loads(request.content)
    with lock: calls.append({"path":request.url.path,"body":body})
    if request.url.path.endswith("/messages"):
        return httpx.Response(200,json={"id":"fixture","type":"message","role":"assistant","model":"claude-haiku-4-5-20251001","content":[{"type":"text","text":"answer"}],"stop_reason":"end_turn","stop_sequence":None,"usage":{"input_tokens":2,"output_tokens":1,"cost":0.01,"is_byok":False}})
    return httpx.Response(200,json={"id":"fixture","object":"chat.completion","created":0,"model":"gpt-4o-mini-2024-07-18","choices":[{"index":0,"finish_reason":"stop","message":{"role":"assistant","content":"answer"}}],"usage":{"prompt_tokens":2,"completion_tokens":1,"total_tokens":3,"cost":0.01,"is_byok":False}})
openai_client=openai.OpenAI(api_key="fixture",max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(handler)))
anthropic_client=anthropic.Anthropic(api_key="fixture",max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(handler)))
requests=[agentc.CompletionRequest(provider,{"model":model,"messages":[{"role":"user","content":provider}],"max_tokens":8},client) for provider,model,client in [("openai","gpt-4o-mini",openai_client),("anthropic","claude-haiku-4-5-20251001",anthropic_client),("litellm","openai/gpt-4o-mini",openai_client)]]
agentc.init()
try:
    with agentc.application_version("prepared-mixed-v1"):
        for mode in ("reference","candidate"):
            responses=agentc.completion_batch(requests,site="mixed",policy=agentc.BatchPolicy(1000),max_workers=3,mode=mode,on_observation=observations.append)
            assert isinstance(responses[0],openai.types.chat.ChatCompletion)
            assert isinstance(responses[1],anthropic.types.Message)
            assert isinstance(responses[2],litellm.ModelResponse)
            assert observations[-1].status=="recorded",observations[-1]
        pair=agentc.compare_parallel_observations(*observations)
        assert pair.status=="recorded" and pair.divergence==0.0,pair
        assert len(calls)==6
        assert [value.sdk_calls for value in observations]==[3,3]
        assert all(call["body"].get("store") is False for call in calls if call["path"].endswith("/chat/completions"))
        print(json.dumps({"http_invocations":len(calls),"observations":[asdict(value) for value in observations],"pair":asdict(pair)}))
finally:
    agentc.shutdown();openai_client.close();anthropic_client.close()
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
    assert json.loads(proc.stdout)["http_invocations"] == 6


@pytest.mark.parametrize("overhead_ms,expected_workers", [(1000, 2), (0, 1)])
def test_prepared_sdk_batches_admit_after_twenty_pairs_and_stop_on_breach(
    tmp_path, overhead_ms, expected_workers
):
    script = r"""
import json,os,threading,time
from dataclasses import asdict
import httpx,openai,agentc
calls, observations, schedules, pairs = [], [], [], []
prepared=[]
native_prepare=agentc._native.optimize_prepare_provider_batch
def capture_prepare(*args):
    raw=native_prepare(*args)
    prepared.append(json.loads(raw))
    return raw
agentc._native.optimize_prepare_provider_batch=capture_prepare
lock=threading.Lock()
active=peak=0
changed=False
fail=False
revision="gpt-4o-mini-2024-07-18"
def handler(request):
    global active,peak
    body=json.loads(request.content)
    with lock:
        calls.append(body);active+=1;peak=max(peak,active)
    time.sleep(0.01)
    with lock: active-=1
    if fail:
        return httpx.Response(500,json={"error":{"message":"fixture failure","type":"server_error"}})
    answer="changed" if changed and body["messages"][0]["content"]=="second" else "answer"
    return httpx.Response(200,json={"id":"fixture","object":"chat.completion","created":0,"model":revision,"choices":[{"index":0,"finish_reason":"stop","message":{"role":"assistant","content":answer}}],"usage":{"prompt_tokens":2,"completion_tokens":1,"total_tokens":3,"cost":0.01,"is_byok":False}})
client=openai.OpenAI(api_key="fixture",max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(handler)))
requests=[agentc.CompletionRequest("openai",{"model":"gpt-4o-mini","messages":[{"role":"user","content":text}],"max_tokens":8},client) for text in ("first","second")]
def run(mode="guarded",remaining=1000,objective="latency"):
    responses=agentc.completion_batch(requests,site="classifier",policy=agentc.BatchPolicy(remaining,objective),max_workers=2,mode=mode,on_observation=observations.append,on_schedule=schedules.append)
    assert len(responses)==2 and all(isinstance(response,openai.types.chat.ChatCompletion) for response in responses)
    return observations[-1]
agentc.init()
try:
    with agentc.application_version("prepared-classifier-v1"):
        run(); assert schedules[-1].selected_reference
        for i in range(19):
            ref,cand=run("reference"),run("candidate")
            pair=agentc.compare_parallel_observations(ref,cand);pairs.append(asdict(pair));assert pair.status=="recorded",pair
        run();assert schedules[-1].selected_reference
        ref,cand=run("reference"),run("candidate")
        pairs.append(asdict(agentc.compare_parallel_observations(ref,cand)))
        run()
        expected_workers=2 if int(os.environ["AGENTC_OPTIMIZE_MAX_OVERHEAD_MS"]) else 1
        assert schedules[-1].max_workers==expected_workers
        assert prepared[-1]["schedule"]["selected_key"]["execution_plan_id"]==observations[-1].execution_plan_id
        if expected_workers==1:
            assert schedules[-1].reason=="planning_overhead_budget"
        admitted=asdict(schedules[-1])
        run(remaining=1);assert schedules[-1].selected_reference
        run(objective="cost");assert schedules[-1].selected_reference
        with agentc.optimization_scope("excluded",optimize=False):
            run();assert schedules[-1].selected_reference
        ref=run("reference");changed=True;cand=run("candidate")
        breach=agentc.compare_parallel_observations(ref,cand);assert breach.divergence==1.0
        run();assert schedules[-1].selected_reference
        changed=False;revision="gpt-4o-mini-new-revision"
        run();run();assert schedules[-1].selected_reference
        fail=True;before=len(calls)
        try: run()
        except openai.APIStatusError: pass
        else: raise AssertionError("original SDK exception was lost")
        assert len(calls)==before+1
        assert observations[-1].reason=="callback_failed"
    assert peak==2 and all(body.get("store") is False for body in calls)
    assert all(not any(str(key).startswith("agentc") for key in body) for body in calls)
    print(json.dumps({"admitted":admitted,"observations":[asdict(value) for value in observations],"schedules":[asdict(value) for value in schedules],"pairs":pairs,"http_invocations":len(calls),"peak_concurrency":peak}))
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
            "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": str(overhead_ms),
            "AGENTC_CAPTURE_CONTENT": "0",
            "AGENTC_CAPTURE_EMBEDDINGS": "0",
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert len(data["pairs"]) == 20
    assert data["admitted"]["max_workers"] == expected_workers
    assert abs(data["admitted"]["completed_acquisition_cost_usd"] - 0.8) < 1e-12
    assert data["http_invocations"] == 103
    assert data["peak_concurrency"] == 2
