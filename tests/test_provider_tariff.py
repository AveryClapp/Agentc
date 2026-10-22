"""Tariff evidence is predeclared and never becomes a provider bill."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from agentc._provider_tariff import request_scope_supported


def text_request():
    return {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "private marker"}],
        "max_tokens": 2048,
    }


@pytest.mark.parametrize(
    "extra",
    [
        {"stream": True},
        {"n": 2},
        {"extra_body": {"future_fee": 1}},
        {"extra_headers": {"anthropic-beta": "future-price"}},
        {"extra_query": {"fee": 1}},
        {"prediction": {"content": "private marker"}},
        {"audio": {}},
        {"tools": [{"type": "web_search"}]},
        {
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "image_url", "image_url": "private marker"}],
                }
            ]
        },
    ],
)
def test_unqualified_request_features_cannot_gain_tariff_evidence(extra):
    assert not request_scope_supported(
        {**text_request(), **extra}, "openai.chat.completions.v1"
    )


def test_text_function_tools_and_known_cache_controls_are_supported():
    assert request_scope_supported(text_request(), "openai.chat.completions.v1")
    assert request_scope_supported(
        {
            **text_request(),
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "lookup",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
        },
        "openai.chat.completions.v1",
    )
    body = {
        **text_request(),
        "model": "claude-haiku-4-5",
        "system": [
            {
                "type": "text",
                "text": "private marker",
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
            }
        ],
        "tools": [{"name": "lookup", "input_schema": {"type": "object"}}],
    }
    assert request_scope_supported(body, "anthropic.messages.v1")
    body["tools"][0]["type"] = "web_search_20250305"
    assert not request_scope_supported(body, "anthropic.messages.v1")


def test_tariff_scope_isolation_restores_provider_request_identity(monkeypatch):
    from agentc._provider_config import declaration
    from agentc import _provider_tariff

    monkeypatch.setattr(
        _provider_tariff,
        "_record",
        lambda operation: {"status": "recorded", "snapshot_sha256": "a" * 64},
    )
    original = declaration(
        text_request(), provider_protocol="openai.chat.completions.v1"
    )
    with _provider_tariff.tariff_accounting():
        scoped = declaration(
            text_request(), provider_protocol="openai.chat.completions.v1"
        )
    assert scoped != original
    assert (
        declaration(text_request(), provider_protocol="openai.chat.completions.v1")
        == original
    )


SDK_SCRIPT = r"""
import asyncio,json
import agentc,openai,anthropic,httpx
from agentc._provider_tariff import active_scope
sends=[];predeclared=[]
def handler(request):
    body=json.loads(request.content);sends.append(body)
    reports=active_scope().reports()
    assert reports and reports[-1]["quantity"]=="published_token_tariff_usd"
    assert any(row["upper_nanodollars"] is None for row in reports)
    predeclared.append(len(reports))
    if request.url.path.endswith("chat/completions"):
        usage={"prompt_tokens":10000,"completion_tokens":1000,"total_tokens":11000,"prompt_tokens_details":{"cached_tokens":4096}}
        data={"id":"chatcmpl-fixture","object":"chat.completion","created":0,"model":"gpt-4o-mini-2024-07-18","service_tier":"default","choices":[{"index":0,"finish_reason":"stop","message":{"role":"assistant","content":"PRIVATE_TARIFF_RESPONSE"}}],"usage":usage}
    else:
        usage={"input_tokens":10000,"output_tokens":1000,"cache_creation_input_tokens":5000,"cache_read_input_tokens":20000,"service_tier":"standard","inference_geo":"global"}
        data={"id":"msg_fixture","type":"message","role":"assistant","model":"claude-haiku-4-5-20251001","content":[{"type":"text","text":"PRIVATE_TARIFF_RESPONSE"}],"stop_reason":"end_turn","stop_sequence":None,"usage":usage}
    return httpx.Response(200,json=data)
agentc.init()
clients=[]
async def run():
    with agentc.tariff_accounting() as scope:
        for asynchronous in [False,True]:
            for provider in ["openai","anthropic"]:
                http=(httpx.AsyncClient if asynchronous else httpx.Client)(transport=httpx.MockTransport(handler))
                sdk=((openai.AsyncOpenAI if asynchronous else openai.OpenAI) if provider=="openai" else (anthropic.AsyncAnthropic if asynchronous else anthropic.Anthropic))(api_key="fixture",max_retries=0,http_client=http)
                clients.append(sdk)
                resource=sdk.chat.completions if provider=="openai" else sdk.messages
                for raw in [False,True]:
                    method=resource.with_raw_response.create if raw else resource.create
                    body={"model":"gpt-4o-mini-2024-07-18" if provider=="openai" else "claude-haiku-4-5-20251001","messages":[{"role":"user","content":"PRIVATE_TARIFF_PROMPT"}],"max_tokens":2048}
                    response=await method(**body) if asynchronous else method(**body)
        reports=scope.reports()
        assert len(reports)==len(sends)==len(predeclared)==8,(reports,sends)
        for report in reports:
            result=report["invocations"][0]["result"]
            assert result["status"]=="bounded",report
            if report["upper_nanodollars"]==1792800:
                assert report["lower_nanodollars"]==1792800
            else:
                assert report["lower_nanodollars"]==23250000 and report["upper_nanodollars"]==27000000,report
            assert len(result["receipt_sha256"])==64
        print(json.dumps({"reports":reports,"sdk_sends":len(sends),"predeclared_before_http":len(predeclared)}))
    for sdk in clients:
        if isinstance(sdk,(openai.AsyncOpenAI,anthropic.AsyncAnthropic)):await sdk.close()
        else:sdk.close()
try:
    asyncio.run(run())
finally:
    agentc.shutdown()
"""


def test_real_sdk_tariffs_bind_before_http_and_survive_restart(tmp_path):
    env = {
        **os.environ,
        "AGENTC_STORAGE_PATH": str(tmp_path),
        "AGENTC_OPTIMIZE": "0",
        "AGENTC_CAPTURE_CONTENT": "0",
        "AGENTC_CAPTURE_EMBEDDINGS": "0",
        "AGENTC_OPTIMIZE_SHADOW": "0",
        "AGENTC_OPTIMIZE_EXPLORATION": "0",
    }
    result = subprocess.run(
        [sys.executable, "-I", "-c", SDK_SCRIPT],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    evidence = json.loads(result.stdout)
    audit_script = r"""
import hashlib,json,sqlite3,sys
from pathlib import Path
conn=sqlite3.connect(Path(sys.argv[1])/"cost_model.db")
rows=list(conn.execute("SELECT invocation_id,invocation_json,completion_json FROM provider_receipt_invocation ORDER BY invocation_id"))
assert len(rows)==8
for id,raw,done in rows:
    inv=json.loads(raw);usage=json.loads(done)
    assert inv["tariff_declaration"]["request_scope_supported"] and usage["cost_source"]=="estimated"
    assert 'PRIVATE_TARIFF_' not in raw+done
assert conn.execute("SELECT COUNT(*) FROM provider_billing_settlement").fetchone()[0]==0
print(json.dumps({"receipts":rows,"cohorts":list(conn.execute("SELECT * FROM provider_tariff_cohort ORDER BY owner"))}))
"""
    audit = subprocess.run(
        [sys.executable, "-I", "-c", audit_script, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert audit.returncode == 0, audit.stderr
    before = json.loads(audit.stdout)
    restart = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            r"""
import agentc,json,sys
agentc.init()
try:
    print(json.dumps([agentc.provider_tariff_report(id) for id in sys.argv[1:]]))
finally:
    agentc.shutdown()
""",
            *[report["batch_id"] for report in evidence["reports"]],
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert restart.returncode == 0, restart.stderr
    assert json.loads(restart.stdout) == evidence["reports"]
    after = subprocess.run(audit.args, capture_output=True, text=True, timeout=30)
    assert after.returncode == 0, after.stderr
    assert json.loads(after.stdout) == before


ACQUISITION_SCRIPT = r"""
import agentc,openai,httpx,json,uuid
captured=[];sends=[]
def handler(request):
    sends.append(1)
    return httpx.Response(200,json={"id":"fixture","object":"chat.completion","created":0,
        "model":"gpt-4o-mini-2024-07-18","service_tier":"default",
        "choices":[{"index":0,"finish_reason":"stop","message":{"role":"assistant","content":"fixture"}}],
        "usage":{"prompt_tokens":10000,"completion_tokens":1000,"total_tokens":11000,
            "prompt_tokens_details":{"cached_tokens":4096}}})
agentc.init()
client=openai.OpenAI(api_key="fixture",max_retries=0,
    http_client=httpx.Client(transport=httpx.MockTransport(handler)))
original=agentc._native.optimize_prepare_provider_batch
def capture(source,options):
    captured.append((source,options))
    return original(source,options)
agentc._native.optimize_prepare_provider_batch=capture
try:
    with agentc.application_version("tariff-acquisition-fixture"),agentc.tariff_accounting():
        observations=[]
        request=agentc.CompletionRequest("openai",{"model":"gpt-4o-mini-2024-07-18",
            "messages":[{"role":"user","content":"fixture"}],"max_tokens":2048},client)
        agentc.completion_batch([request],site="tariff-owner",policy=agentc.BatchPolicy(1000),
            mode="reference",on_observation=observations.append)
        batch=observations[0].provider_receipt_batch_id
        owner=agentc.provider_tariff_report(batch)["acquisition"]["owner"]
        def summary():
            return json.loads(agentc._native.optimize_record_provider_receipt(json.dumps({
                "operation":"tariff_acquisition","call_site_version":owner})))["acquisition"]
        ready=summary()
        assert ready["upper_nanodollars"]==1792800 and ready["coverage_complete"],ready
        for _ in range(10):assert summary()==ready
        source,options=captured[0]
        options=json.loads(options);options["provider_receipt_batch_id"]=uuid.uuid4().hex
        original(source,json.dumps(options))
        pending=summary()
        assert pending["upper_nanodollars"] is None and not pending["coverage_complete"],pending
        assert pending["observed_charged_lower_nanodollars"]==1792800
        for _ in range(10):assert summary()==pending
        for operation,extra in [("seal_batch",dict(logical_calls=[],persistence_failed=False)),
                ("finish_batch_economics",dict(wall_latency_ms=0.0))]:
            result=json.loads(agentc._native.optimize_record_provider_receipt(json.dumps(
                dict(operation=operation,batch_id=options["provider_receipt_batch_id"],**extra))))
            assert result["status"]=="recorded",result
        final=summary()
        assert final["upper_nanodollars"]==1792800 and final["coverage_complete"],final
        assert final["owned_batches"]==final["coverage_batches"]==2
        assert len(sends)==1,sends
        print(json.dumps(dict(owner=owner,batch_id=batch,pending_batch_id=options["provider_receipt_batch_id"],
            sdk_sends=len(sends),ready=ready,pending=pending,final=final)))
finally:
    agentc._native.optimize_prepare_provider_batch=original
    agentc.shutdown();client.close()
"""


def test_real_sdk_acquisition_summary_preserves_reservations_and_restart(tmp_path):
    env = {
        **os.environ,
        "AGENTC_STORAGE_PATH": str(tmp_path),
        "AGENTC_OPTIMIZE": "0",
        "AGENTC_CAPTURE_CONTENT": "0",
        "AGENTC_CAPTURE_EMBEDDINGS": "0",
        "AGENTC_OPTIMIZE_SHADOW": "0",
        "AGENTC_OPTIMIZE_EXPLORATION": "0",
    }
    result = subprocess.run(
        [sys.executable, "-I", "-c", ACQUISITION_SCRIPT],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    proof = json.loads(result.stdout)
    audit_script = r"""
import json,sqlite3,sys
from pathlib import Path
assert not any(n=='agentc' or n.startswith('agentc.') for n in sys.modules)
conn=sqlite3.connect(Path(sys.argv[1])/"cost_model.db")
assert conn.execute("SELECT COUNT(*) FROM provider_receipt_invocation").fetchone()[0]==1
assert conn.execute("SELECT COUNT(*) FROM provider_billing_settlement").fetchone()[0]==0
assert all(r[0]==0 for r in conn.execute("SELECT n_paired_observations FROM execution_plan_profile"))
coverage=list(conn.execute("SELECT batch_id,owner,complete FROM provider_tariff_coverage ORDER BY batch_id"))
assert len(coverage)==2 and all(row[2]==1 for row in coverage)
print(json.dumps(dict(
    receipts=list(conn.execute("SELECT invocation_id,invocation_json,completion_json FROM provider_receipt_invocation")),
    coverage=coverage,
    cohorts=list(conn.execute("SELECT * FROM provider_tariff_coverage_cohort ORDER BY owner")),
    tariffs=list(conn.execute("SELECT * FROM provider_tariff_invocation ORDER BY invocation_id"))
)))
"""
    before = subprocess.run(
        [sys.executable, "-I", "-c", audit_script, str(tmp_path)],
        capture_output=True, text=True, timeout=30,
    )
    assert before.returncode == 0, before.stderr
    restart_script = r"""
import agentc,json,sys
agentc.init()
try:
    result=json.loads(agentc._native.optimize_record_provider_receipt(json.dumps({
        "operation":"tariff_acquisition","call_site_version":sys.argv[1]})))
    print(json.dumps(result["acquisition"]))
finally:
    agentc.shutdown()
"""
    restart = subprocess.run(
        [sys.executable, "-I", "-c", restart_script, proof["owner"]],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert restart.returncode == 0, restart.stderr
    assert json.loads(restart.stdout) == proof["final"]
    after = subprocess.run(before.args, capture_output=True, text=True, timeout=30)
    assert after.returncode == 0, after.stderr
    assert json.loads(after.stdout) == json.loads(before.stdout)


PLAN_PROFILE_SCRIPT = r"""
import agentc,openai,anthropic,httpx,json
sends=[]
def handler(request):
    sends.append(1)
    if request.url.path.endswith("chat/completions"):
        usage={"prompt_tokens":10000,"completion_tokens":1000,"total_tokens":11000,"prompt_tokens_details":{"cached_tokens":4096}}
        body={"id":"chatcmpl-fixture","object":"chat.completion","created":0,"model":"gpt-4o-mini-2024-07-18","service_tier":"default",
            "choices":[{"index":0,"finish_reason":"stop","message":{"role":"assistant","content":"PRIVATE_PLAN_RESPONSE"}}],"usage":usage}
    else:
        usage={"input_tokens":10000,"output_tokens":1000,"cache_creation_input_tokens":5000,"cache_read_input_tokens":20000,
            "service_tier":"standard","inference_geo":"global"}
        body={"id":"msg_fixture","type":"message","role":"assistant","model":"claude-haiku-4-5-20251001",
            "content":[{"type":"text","text":"PRIVATE_PLAN_RESPONSE"}],"stop_reason":"end_turn","stop_sequence":None,"usage":usage}
    return httpx.Response(200,json=body)
agentc.init()
clients=[openai.OpenAI(api_key="fixture",max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(handler))),
    anthropic.Anthropic(api_key="fixture",max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(handler)))]
try:
    reports=[]
    with agentc.application_version("tariff-plan-profile-fixture"),agentc.tariff_accounting():
        requests=[agentc.CompletionRequest(provider,{"model":model,"messages":[{"role":"user","content":"PRIVATE_PLAN_PROMPT"}],
                "max_tokens":2048},client)
            for provider,model,client in zip(["openai","anthropic"],
                ["gpt-4o-mini-2024-07-18","claude-haiku-4-5-20251001"],clients)]
        for run in range(2):
            observations=[]
            results=agentc.completion_batch(requests,site="interval-profile",policy=agentc.BatchPolicy(1000),mode="reference",
                on_observation=observations.append)
            assert len(results)==2 and observations[0].status=="pending_billing",observations
            report=agentc.provider_tariff_plan_report(observations[0].provider_receipt_batch_id)
            assert report["admission"]=="observe_only" and report["quality_evidence"]=="separate_original_comparisons_required"
            row=report["observation"];window=report["window"]
            assert row and len(row["receipts"])==2 and row["original_comparison"],report
            expected={"lower_nanodollars":25042800,"upper_nanodollars":28792800}
            assert row["bound"]["interval"]==window["mean_interval"]==expected,report
            assert window["lifetime_observations"]==len(window["observations"])==run+1,report
            assert agentc.provider_tariff_plan_report(row["batch_id"])==report
            assert 'PRIVATE_PLAN_' not in json.dumps(report)
            reports.append(report)
        assert len(sends)==4,sends
        print(json.dumps(dict(sdk_sends=len(sends),reports=reports)))
finally:
    agentc.shutdown()
    for client in clients:client.close()
"""


def test_original_mixed_provider_plan_intervals_do_not_publish_usd_or_quality(tmp_path):
    env = {
        **os.environ,
        "AGENTC_STORAGE_PATH": str(tmp_path),
        "AGENTC_OPTIMIZE": "0",
        "AGENTC_CAPTURE_CONTENT": "0",
        "AGENTC_CAPTURE_EMBEDDINGS": "0",
        "AGENTC_OPTIMIZE_SHADOW": "0",
        "AGENTC_OPTIMIZE_EXPLORATION": "0",
        "AGENTC_OPTIMIZE_PLAN_PROFILE_WINDOW": "2",
    }
    result = subprocess.run(
        [sys.executable, "-I", "-c", PLAN_PROFILE_SCRIPT],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    proof = json.loads(result.stdout)
    audit_script = r"""
import json,sqlite3,sys
from pathlib import Path
assert not any(n=='agentc' or n.startswith('agentc.') for n in sys.modules)
conn=sqlite3.connect(Path(sys.argv[1])/"cost_model.db")
assert conn.execute("SELECT COUNT(*) FROM provider_receipt_invocation").fetchone()[0]==4
assert conn.execute("SELECT COUNT(*) FROM provider_tariff_plan_observation").fetchone()[0]==2
assert conn.execute("SELECT lifetime_observations FROM provider_tariff_plan_state").fetchone()[0]==2
assert conn.execute("SELECT COUNT(*) FROM provider_billing_settlement").fetchone()[0]==0
assert conn.execute("SELECT COUNT(*) FROM multi_call_comparison_pair").fetchone()[0]==0
assert all(r[0]==0 for r in conn.execute("SELECT n_paired_observations FROM execution_plan_profile"))
assert all(json.loads(r[0])["source"]=="estimated" for r in conn.execute("SELECT cost_evidence_json FROM execution_plan_observation"))
assert conn.execute("SELECT COUNT(*) FROM execution_plan_observation").fetchone()[0]==4
print(json.dumps(dict(
    receipts=list(conn.execute("SELECT invocation_id,invocation_json,completion_json FROM provider_receipt_invocation ORDER BY invocation_id")),
    observations=list(conn.execute("SELECT * FROM provider_tariff_plan_observation ORDER BY source_sequence")),
    states=list(conn.execute("SELECT * FROM provider_tariff_plan_state"))
)))
"""
    before = subprocess.run(
        [sys.executable, "-I", "-c", audit_script, str(tmp_path)],
        capture_output=True, text=True, timeout=30,
    )
    assert before.returncode == 0, before.stderr
    restart_script = r"""
import agentc,json,sys
agentc.init()
try:
    print(json.dumps(agentc.provider_tariff_plan_report(sys.argv[1])))
finally:
    agentc.shutdown()
"""
    restart = subprocess.run(
        [sys.executable, "-I", "-c", restart_script, proof["reports"][-1]["observation"]["batch_id"]],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert restart.returncode == 0, restart.stderr
    assert json.loads(restart.stdout) == proof["reports"][-1]
    after = subprocess.run(before.args, capture_output=True, text=True, timeout=30)
    assert after.returncode == 0, after.stderr
    assert json.loads(after.stdout) == json.loads(before.stdout)


ORIGINAL_PAIR_SCRIPT = r"""
import agentc,openai,anthropic,httpx,json
sends=[]
def handler(request):
    sends.append(request.url.path)
    if request.url.path.endswith('chat/completions'):
        body={'id':'chatcmpl-pair','object':'chat.completion','created':0,'model':'gpt-4o-mini-2024-07-18',
            'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':'PRIVATE_ORIGINAL_RESULT'}}],
            'usage':{'prompt_tokens':2,'completion_tokens':1,'total_tokens':3}}
    else:
        body={'id':'msg_pair','type':'message','role':'assistant','model':'claude-haiku-4-5-20251001',
            'content':[{'type':'text','text':'PRIVATE_ORIGINAL_RESULT'}],'stop_reason':'end_turn','stop_sequence':None,
            'usage':{'input_tokens':2,'output_tokens':1,'cache_creation_input_tokens':0,'cache_read_input_tokens':0}}
    return httpx.Response(200,json=body)
agentc.init()
clients=[openai.OpenAI(api_key='fixture',max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(handler))),
    anthropic.Anthropic(api_key='fixture',max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(handler)))]
try:
    executions=[];reports=[]
    with agentc.application_version('original-pair-v1'),agentc.tariff_accounting():
        requests=[agentc.CompletionRequest(provider,{'model':model,'messages':[{'role':'user','content':'PRIVATE_ORIGINAL_PROMPT'}],
                'max_tokens':16},client)
            for provider,model,client in zip(['openai','anthropic'],
                ['gpt-4o-mini-2024-07-18','claude-haiku-4-5-20251001'],clients)]
        for mode in ['reference','candidate']:
            observations=[]
            results=agentc.completion_batch(requests,site='original-pair',policy=agentc.BatchPolicy(10000),mode=mode,max_workers=2,
                on_observation=observations.append)
            assert len(results)==2 and observations[0].status=='pending_billing',observations
            batch=observations[0].provider_receipt_batch_id
            execution=agentc.provider_original_execution(batch)
            report=agentc.provider_tariff_plan_report(batch)
            assert execution['execution_id']==report['observation']['original_execution_id']
            assert len(execution['receipts'])==2 and execution['original_comparison']['max_workers']==(1 if mode=='reference' else 2)
            assert agentc.provider_original_execution(batch)==execution
            executions.append(execution);reports.append(report)
        pair=agentc.compare_original_provider_executions(*(row['execution_id'] for row in executions))
        assert pair['divergence']==0 and agentc.compare_original_provider_executions(*(row['execution_id'] for row in executions))==pair
        assert len(sends)==4 and 'PRIVATE_ORIGINAL_' not in json.dumps([executions,reports,pair])
        print(json.dumps(dict(sdk_sends=len(sends),executions=executions,reports=reports,pair=pair)))
finally:
    agentc.shutdown()
    for client in clients:client.close()
"""


def test_original_mixed_provider_results_pair_once_without_billing(tmp_path):
    env = {
        **os.environ,
        "AGENTC_STORAGE_PATH": str(tmp_path),
        "AGENTC_OPTIMIZE": "0",
        "AGENTC_CAPTURE_CONTENT": "0",
        "AGENTC_CAPTURE_EMBEDDINGS": "0",
        "AGENTC_OPTIMIZE_SHADOW": "0",
        "AGENTC_OPTIMIZE_EXPLORATION": "0",
        "AGENTC_OPTIMIZE_PLAN_PROFILE_WINDOW": "2",
    }
    result = subprocess.run(
        [sys.executable, "-I", "-c", ORIGINAL_PAIR_SCRIPT],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    proof = json.loads(result.stdout)
    audit_script = r"""
import hashlib,json,sqlite3,sys
from pathlib import Path
assert not any(n=='agentc' or n.startswith('agentc.') for n in sys.modules)
conn=sqlite3.connect(Path(sys.argv[1])/'cost_model.db')
canonical=lambda value:json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False)
digest=lambda value:hashlib.sha256(value.encode()).hexdigest()
executions=[]
for identity,raw,hash_ in conn.execute('SELECT execution_id,execution_json,execution_sha256 FROM provider_original_execution ORDER BY source_sequence'):
    assert digest(raw)==hash_
    execution=json.loads(raw)
    assert digest(canonical({key:execution[key] for key in ['version','batch_id','input_sha256','journal_sha256']}))==identity
    input_hash,input_raw,sequence=conn.execute('SELECT input_sha256,input_json,rowid FROM provider_receipt_publication_input WHERE batch_id=?',(execution['batch_id'],)).fetchone()
    original=json.loads(input_raw)
    assert input_hash==digest(input_raw)==execution['input_sha256'] and sequence==execution['source_sequence']
    assert original['comparison']==execution['original_comparison']
    for receipt in execution['receipts']:
        invocation,completion=conn.execute('SELECT invocation_json,completion_json FROM provider_receipt_invocation WHERE invocation_id=?',(receipt['invocation_id'],)).fetchone()
        assert digest(canonical(dict(version='provider-billing-binding-v1',batch_id=execution['batch_id'],invocation_id=receipt['invocation_id'],invocation=invocation,completion=completion)))==receipt['receipt_sha256']
    executions.append(execution)
assert len(executions)==2
assert conn.execute('SELECT COUNT(*) FROM provider_receipt_invocation').fetchone()[0]==4
assert conn.execute('SELECT SUM(lifetime_executions) FROM provider_original_execution_state').fetchone()[0]==2
assert conn.execute('SELECT COUNT(*) FROM provider_original_execution_pair').fetchone()[0]==1
identity,raw,hash_=conn.execute('SELECT pair_id,pair_json,pair_sha256 FROM provider_original_execution_pair').fetchone()
assert digest(raw)==hash_
pair=json.loads(raw)
assert pair['pair_id']==identity==digest(canonical(dict(version='provider-original-pair-v1',reference=executions[0]['execution_id'],candidate=executions[1]['execution_id'])))
assert pair['reference_execution_id']==executions[0]['execution_id'] and pair['candidate_execution_id']==executions[1]['execution_id']
assert conn.execute('SELECT COUNT(*) FROM provider_billing_settlement').fetchone()[0]==0
assert conn.execute('SELECT COUNT(*) FROM multi_call_comparison_pair').fetchone()[0]==0
assert conn.execute('SELECT COUNT(*) FROM provider_original_usd_comparison').fetchone()[0]==0
assert all(r[0]==0 for r in conn.execute('SELECT n_paired_observations FROM execution_plan_profile'))
assert conn.execute('SELECT COUNT(*) FROM execution_plan_observation').fetchone()[0]==4
assert all(json.loads(r[0])['source']=='estimated' for r in conn.execute('SELECT cost_evidence_json FROM execution_plan_observation'))
reports=list(conn.execute('SELECT observation_json FROM provider_tariff_plan_observation ORDER BY source_sequence'))
assert len(reports)==2 and [json.loads(row[0])['original_execution_id'] for row in reports]==[row['execution_id'] for row in executions]
print(json.dumps(dict(executions=executions,pair=pair,reports=reports)))
"""
    before = subprocess.run(
        [sys.executable, "-I", "-c", audit_script, str(tmp_path)],
        capture_output=True, text=True, timeout=30,
    )
    assert before.returncode == 0, before.stderr
    audited = json.loads(before.stdout)
    assert audited["executions"] == proof["executions"]
    assert audited["pair"] == proof["pair"]
    restart_script = r"""
import agentc,json,sys
agentc.init()
try:
    executions=[agentc.provider_original_execution(batch) for batch in sys.argv[1:]]
    pair=agentc.compare_original_provider_executions(*(row['execution_id'] for row in executions))
    print(json.dumps(dict(executions=executions,pair=pair)))
finally:
    agentc.shutdown()
"""
    restart = subprocess.run(
        [sys.executable, "-I", "-c", restart_script, *(row["batch_id"] for row in proof["executions"])],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert restart.returncode == 0, restart.stderr
    assert json.loads(restart.stdout) == {"executions": proof["executions"], "pair": proof["pair"]}
    after = subprocess.run(before.args, capture_output=True, text=True, timeout=30)
    assert after.returncode == 0, after.stderr
    assert json.loads(after.stdout) == audited
