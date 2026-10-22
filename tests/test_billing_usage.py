"""Original billable categories survive SDK capture without becoming USD bills."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from agentc._billing_usage import ANTHROPIC, OPENAI, VERSION, projection


def response(usage, **kwargs):
    return SimpleNamespace(usage=usage, **kwargs)


def test_missing_categories_are_distinct_from_reported_zero():
    absent = projection(response({"prompt_tokens": 4, "completion_tokens": 2}), OPENAI)
    zero = projection(
        response(
            {
                "prompt_tokens": 4,
                "completion_tokens": 2,
                "prompt_tokens_details": {"cached_tokens": 0},
            }
        ),
        OPENAI,
    )
    assert absent["usage"]["input_details"] is None
    assert absent["usage"]["service_tier"] is None
    assert zero["usage"]["input_details"]["cached_tokens"] == 0
    assert zero["usage"]["input_details"]["cache_write_tokens"] is None


def test_anthropic_cached_tokens_are_separate_from_uncached_input():
    value = projection(
        response(
            {
                "input_tokens": 3,
                "output_tokens": 2,
                "cache_creation_input_tokens": 100,
                "cache_read_input_tokens": 900,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 40,
                    "ephemeral_1h_input_tokens": 60,
                },
                "server_tool_use": {"web_search_requests": 2, "web_fetch_requests": 1},
                "service_tier": "priority",
                "inference_geo": "us",
            },
            service_tier="standard",
        ),
        ANTHROPIC,
    )
    assert value["version"] == VERSION
    assert value["usage"]["input_tokens"] == 3
    assert value["usage"]["cache_read_input_tokens"] == 900
    assert value["usage"]["cache_creation"]["ephemeral_1h_input_tokens"] == 60
    assert value["usage"]["service_tier"] == "priority"
    assert value["usage"]["unrecognized_fields"] is False


@pytest.mark.parametrize(
    "protocol,usage",
    [
        (OPENAI, {"prompt_tokens": True, "completion_tokens": 2}),
        (OPENAI, {"prompt_tokens": -1, "completion_tokens": 2}),
        (OPENAI, {"prompt_tokens": 2**32, "completion_tokens": 2}),
        (OPENAI, {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 7}),
        (
            OPENAI,
            {
                "prompt_tokens": 4,
                "completion_tokens": 2,
                "prompt_tokens_details": {"cached_tokens": 5},
            },
        ),
        (
            OPENAI,
            {
                "prompt_tokens": 4,
                "completion_tokens": 2,
                "completion_tokens_details": {"rejected_prediction_tokens": 3},
            },
        ),
        (
            OPENAI,
            {
                "prompt_tokens": 4,
                "completion_tokens": 2,
                "prompt_tokens_details": "private",
            },
        ),
        (
            OPENAI,
            {
                "prompt_tokens": 4,
                "completion_tokens": 2,
                "prompt_tokens_details": {"cached_tokens": 1.0},
            },
        ),
        (
            ANTHROPIC,
            {"input_tokens": 3, "output_tokens": 2, "cache_read_input_tokens": "900"},
        ),
        (
            ANTHROPIC,
            {
                "input_tokens": 3,
                "output_tokens": 2,
                "cache_creation_input_tokens": 100,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 40,
                    "ephemeral_1h_input_tokens": 61,
                },
            },
        ),
        (
            ANTHROPIC,
            {
                "input_tokens": 3,
                "output_tokens": 2,
                "cache_creation_input_tokens": 100,
                "cache_creation": {"ephemeral_5m_input_tokens": 101},
            },
        ),
        (
            ANTHROPIC,
            {
                "input_tokens": 3,
                "output_tokens": 2,
                "server_tool_use": {"web_search_requests": False},
            },
        ),
        (ANTHROPIC, {"input_tokens": 3}),
    ],
)
def test_malformed_usage_cannot_become_bound_evidence(protocol, usage):
    assert projection(response(usage), protocol) is None


@pytest.mark.parametrize(
    "protocol,usage",
    [
        (
            OPENAI,
            {
                "prompt_tokens": 4,
                "completion_tokens": 2,
                "future_fee": "private marker",
            },
        ),
        (
            OPENAI,
            {
                "prompt_tokens": 4,
                "completion_tokens": 2,
                "prompt_tokens_details": {"new_category": "private marker"},
            },
        ),
        (
            ANTHROPIC,
            {
                "input_tokens": 3,
                "output_tokens": 2,
                "iterations": [{"content": "private marker"}],
            },
        ),
        (
            ANTHROPIC,
            {
                "input_tokens": 3,
                "output_tokens": 2,
                "server_tool_use": {"new_tool": "private marker"},
            },
        ),
    ],
)
def test_new_usage_fields_flag_uncertainty_without_retaining_contents(protocol, usage):
    value = projection(response(usage), protocol)
    assert value["usage"]["unrecognized_fields"] is True
    assert "private marker" not in json.dumps(value)


def test_unknown_tier_is_not_retained_as_arbitrary_provider_content():
    value = projection(
        response(
            {"prompt_tokens": 4, "completion_tokens": 2}, service_tier="private marker"
        ),
        OPENAI,
    )
    assert value["usage"]["service_tier"] is None
    assert value["usage"]["unrecognized_fields"] is True
    assert "private marker" not in json.dumps(value)


def test_usage_projection_failure_does_not_escape():
    class Broken:
        def model_dump(self):
            raise RuntimeError("broken SDK usage")

    assert projection(response(Broken()), OPENAI) is None
    assert projection(response({}), "other.protocol") is None


SDK_SCRIPT = r"""
import asyncio,json
import agentc,anthropic,httpx,openai
from agentc._provider_receipts import ProviderReceiptBatch,instrument_provider,note_logical_call
calls=[]
def handler(request):
    calls.append(str(request.url.path))
    if request.url.path.endswith("chat/completions"):
        data={"id":"chatcmpl-fixture","object":"chat.completion","created":0,"model":"gpt-4o-mini-2024-07-18","service_tier":"priority","choices":[{"index":0,"finish_reason":"stop","message":{"role":"assistant","content":"private response marker"}}],"usage":{"prompt_tokens":100,"completion_tokens":12,"total_tokens":112,"prompt_tokens_details":{"cached_tokens":70,"cache_write_tokens":20,"audio_tokens":0,"image_tokens":0,"text_tokens":100},"completion_tokens_details":{"accepted_prediction_tokens":3,"rejected_prediction_tokens":2,"audio_tokens":0,"reasoning_tokens":4,"text_tokens":12}}}
    else:
        data={"id":"msg_fixture","type":"message","role":"assistant","model":"claude-sonnet-4-5-20250929","content":[{"type":"text","text":"private response marker"}],"stop_reason":"end_turn","stop_sequence":None,"usage":{"input_tokens":3,"output_tokens":12,"cache_creation_input_tokens":100,"cache_read_input_tokens":900,"cache_creation":{"ephemeral_5m_input_tokens":40,"ephemeral_1h_input_tokens":60},"server_tool_use":{"web_search_requests":2,"web_fetch_requests":1},"service_tier":"priority","inference_geo":"us"}}
    return httpx.Response(200,json=data,headers={"x-request-id":"fixture-request"})
clients=[]
entries=[]
for asynchronous in [False,True]:
    http=(httpx.AsyncClient if asynchronous else httpx.Client)(transport=httpx.MockTransport(handler))
    sdk=(openai.AsyncOpenAI if asynchronous else openai.OpenAI)(api_key="fixture",max_retries=0,http_client=http)
    clients.append(sdk)
    for raw in [False,True]:
        resource=sdk.chat.completions
        method=resource.with_raw_response.create if raw else resource.create
        entries.append((asynchronous,raw,"openai.chat.completions.v1",resource,method,{"model":"gpt-4o-mini-2024-07-18","messages":[{"role":"user","content":"private prompt marker"}],"max_tokens":20,"service_tier":"default"}))
    http=(httpx.AsyncClient if asynchronous else httpx.Client)(transport=httpx.MockTransport(handler))
    sdk=(anthropic.AsyncAnthropic if asynchronous else anthropic.Anthropic)(api_key="fixture",max_retries=0,http_client=http)
    clients.append(sdk)
    for raw in [False,True]:
        resource=sdk.messages
        method=resource.with_raw_response.create if raw else resource.create
        entries.append((asynchronous,raw,"anthropic.messages.v1",resource,method,{"model":"claude-sonnet-4-5-20250929","messages":[{"role":"user","content":"private prompt marker"}],"max_tokens":20}))
agentc.init()
ids=[]
async def run():
    for asynchronous,raw,protocol,resource,method,body in entries:
        batch=ProviderReceiptBatch("a"*64,"billable-usage-fixture",1)
        with batch.item(0):
            note_logical_call(0)
            call=instrument_provider(method,resource,protocol,asynchronous=asynchronous)
            result=await call(**body) if asynchronous else call(**body)
        summary=batch.seal([1])
        assert not batch.failed,(protocol,asynchronous,raw)
        assert summary["sdk_provider_invocations"]==1,summary
        assert summary["known_http_attempts"]==1,summary
        assert summary["known_provider_cost_usd"]==0 and not summary["cost_complete"],summary
        ids.append({"batch_id":batch.batch_id,"protocol":protocol,"async":asynchronous,"raw":raw})
    for sdk in clients:
        if isinstance(sdk,(openai.AsyncOpenAI,anthropic.AsyncAnthropic)):
            await sdk.close()
        else:
            sdk.close()
try:
    asyncio.run(run())
    assert len(calls)==8,calls
    print(json.dumps({"batches":ids,"http_sends":len(calls)}))
finally:
    agentc.shutdown()
"""


def test_real_sdk_usage_survives_raw_async_and_restart_without_repricing(tmp_path):
    env = {
        **os.environ,
        "AGENTC_STORAGE_PATH": str(tmp_path),
        "AGENTC_OPTIMIZE": "0",
        "AGENTC_CAPTURE_CONTENT": "0",
        "AGENTC_CAPTURE_EMBEDDINGS": "0",
    }
    proc = subprocess.run(
        [sys.executable, "-I", "-c", SDK_SCRIPT],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    # SQL is deliberately inspected in a separate process without the SDK/native module.
    audit = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            r"""
import json,sqlite3,sys
from pathlib import Path
store=Path(sys.argv[1])
dbs=list(store.rglob("*.db"))+list(store.rglob("*.sqlite"))+list(store.rglob("*.sqlite3"))
rows=[]
for path in dbs:
    with sqlite3.connect(path) as conn:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='provider_receipt_invocation'").fetchone():
            rows.extend(conn.execute("SELECT batch_id,invocation_json,completion_json FROM provider_receipt_invocation"))
print(json.dumps(rows))
""",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert audit.returncode == 0, audit.stderr
    rows = json.loads(audit.stdout)
    assert len(rows) == data["http_sends"] == 8
    originals = {}
    for batch_id, invocation_json, completion_json in rows:
        invocation, completion = (
            json.loads(invocation_json),
            json.loads(completion_json),
        )
        assert "private prompt marker" not in invocation_json + completion_json
        assert "private response marker" not in invocation_json + completion_json
        assert completion["cost_source"] == "estimated"
        assert completion["http_attempts"] == 1
        usage = completion["billing_usage"]["usage"]
        assert completion["billing_usage"]["version"] == VERSION
        assert usage["provider_protocol"] == invocation["provider_protocol"]
        assert usage["service_tier"] == "priority"
        assert usage["unrecognized_fields"] is False
        if usage["provider_protocol"] == OPENAI:
            assert usage["input_details"]["cached_tokens"] == 70
            assert usage["input_details"]["cache_write_tokens"] == 20
            assert usage["output_details"]["rejected_prediction_tokens"] == 2
        else:
            assert (
                usage["input_tokens"] == 3 and usage["cache_read_input_tokens"] == 900
            )
            assert usage["cache_creation"]["ephemeral_1h_input_tokens"] == 60
            assert usage["server_tool_use"]["web_search_requests"] == 2
        originals[batch_id] = completion_json
    restart = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            r"""
import agentc,json,sys
agentc.init()
try:
    result=[json.loads(agentc._native.optimize_record_provider_receipt(json.dumps({"operation":"report","batch_id":id})))["summary"] for id in sys.argv[1:]]
    print(json.dumps(result))
finally:
    agentc.shutdown()
""",
            *originals,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert restart.returncode == 0, restart.stderr
    assert all(
        not row["cost_complete"] and row["known_provider_cost_usd"] == 0
        for row in json.loads(restart.stdout)
    )
    repeated = subprocess.run(audit.args, capture_output=True, text=True, timeout=30)
    assert repeated.returncode == 0, repeated.stderr
    assert {
        id: completion for id, _, completion in json.loads(repeated.stdout)
    } == originals
