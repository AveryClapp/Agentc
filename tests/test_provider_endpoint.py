"""Actual endpoint identity separates catalog and accounting cohorts."""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from types import SimpleNamespace

import pytest

import agentc
from agentc import _native
from agentc._patches._optimizer_glue import build_call_dict_openai
from agentc._provider_endpoint import declaration, validate_namespace


@pytest.fixture(autouse=True)
def optimizer(tmp_path, monkeypatch):
    for key, value in {
        "AGENTC_OPTIMIZE": "1",
        "AGENTC_OPTIMIZE_EXPLORATION": "0",
        "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": "1000",
    }.items():
        monkeypatch.setenv(key, value)
    _native.optimize_reset()
    _native.optimize_configure(str(tmp_path))
    yield
    _native.optimize_reset()


def call(endpoint, model, protocol="openai.chat.completions.v1"):
    with agentc.application_version("endpoint-fixture-v1"):
        result = build_call_dict_openai(
            {
                "model": model,
                "messages": [{"role": "user", "content": "private task"}],
                "max_tokens": 16,
            },
            call_site_id="endpoint-fixture",
            trace_id_hex="00" * 16,
            span_id_hex="01" * 8,
            provider_protocol=protocol,
            client=SimpleNamespace(base_url=endpoint),
        )
    return result


def prepare(source, mode="guarded"):
    source = copy.deepcopy(source)
    source["parameters"]["extra"]["agentc_prepared_provider_batch"] = {
        "version": "independent-provider-requests-v1",
        "provider_protocol": source["parameters"]["extra"]["agentc_route_context"][
            "provider_protocol"
        ],
        "retry_limit": 0,
        "application_callbacks": False,
        "remote_store": False,
    }
    app = source["parameters"]["extra"]["agentc_route_context"]["application_version"][
        "sha256"
    ]
    return json.loads(
        _native.optimize_prepare_provider_batch(
            json.dumps(
                {
                    "application_version": app,
                    "callback_version": "endpoint-fixture-v1",
                    "calls": [source],
                }
            ),
            json.dumps(
                {
                    "provider_receipt_batch_id": uuid.uuid4().hex,
                    "max_workers": 2,
                    "remaining_batches": 1000,
                    "objective": "latency",
                    "mode": mode,
                    "scope_eligible": True,
                    "max_expected_cost_usd": None,
                    "max_expected_latency_ms": None,
                }
            ),
        )
    )


@pytest.mark.parametrize(
    "endpoint,protocol,expected",
    [
        ("https://api.openai.com/v1/", "openai.chat.completions.v1", "openai"),
        ("https://openrouter.ai/api/v1", "openai.chat.completions.v1", "openrouter"),
        ("https://openrouter.ai/api/v1", "litellm.completion.v1", "openrouter"),
        ("https://api.anthropic.com/", "anthropic.messages.v1", "anthropic"),
    ],
)
def test_sdk_endpoint_not_model_prefix_declares_namespace(endpoint, protocol, expected):
    value = declaration(SimpleNamespace(base_url=endpoint), protocol)
    assert value["supported"] and validate_namespace(value, protocol) == expected
    assert (
        value["sha256"]
        == hashlib.sha256(
            ("billing-scope-v1\0" + endpoint.rstrip("/")).encode()
        ).hexdigest()
    )


def test_endpoint_digest_separates_same_call_site_and_retains_no_private_url():
    direct = call("https://api.openai.com/v1", "gpt-4o-mini")
    router = call("https://openrouter.ai/api/v1", "openai/gpt-4o-mini")
    a = json.loads(_native.optimize_plan(json.dumps(direct)))[
        "agentc_observation_context"
    ]
    b = json.loads(_native.optimize_plan(json.dumps(router)))[
        "agentc_observation_context"
    ]
    assert a["key"]["call_site_version"] != b["key"]["call_site_version"]
    assert b["provider_alias"] is True
    assert b["runtime_version"]["target_model_id"] == "openai/gpt-4o-mini"
    private = call(
        "https://PRIVATE_USER:PRIVATE_KEY@private.invalid/api/v1?PRIVATE_QUERY",
        "gpt-4o-mini",
    )
    context = private["parameters"]["extra"]["agentc_route_context"]
    assert context["provider_namespace"].startswith("unqualified:")
    assert "PRIVATE" not in json.dumps(context["provider_endpoint"])


@pytest.mark.parametrize(
    "model,protocol",
    [
        ("openai/gpt-4o-mini", "openai.chat.completions.v1"),
        ("openai/gpt-4o", "openai.chat.completions.v1"),
        ("openai/openai/gpt-4o-mini", "litellm.completion.v1"),
    ],
)
def test_router_slug_prepares_original_with_catalog_observation_and_cold_admission(
    model, protocol
):
    result = prepare(call("https://openrouter.ai/api/v1", model, protocol))
    assert result["status"] == "prepared", result
    assert (
        result["max_workers"] == 1
        and result["schedule"]["reason"] == "provider_alias_unresolved"
    )
    context = result["plans"][0]["agentc_observation_context"]
    assert (
        context["provider_alias"]
        and context["runtime_version"]["target_model_id"] == model
    )
    assert (
        "@agentc-model-catalog-" in context["runtime_version"]["target_model_version"]
    )


@pytest.mark.parametrize(
    "endpoint,model",
    [
        ("https://api.openai.com/v1", "openai/gpt-4o-mini"),
        ("https://openrouter.ai/api/v1", "gpt-4o-mini"),
        ("https://openrouter.ai/api/v1", "vendor/uncataloged-model"),
        ("https://private.invalid/api/v1", "gpt-4o-mini"),
    ],
)
def test_uncataloged_original_has_an_accounting_owner_and_no_quality_admission(
    endpoint, model
):
    source = call(endpoint, model)
    result = prepare(source)
    assert result["status"] == "prepared", result
    assert (
        result["schedule"]["reason"] == "model_catalog_unqualified"
        and result["max_workers"] == 1
    )
    assert (
        result["plans"][0]["agentc_observation_context"]["runtime_version"][
            "price_table_version"
        ]
        == "unqualified-original-accounting-v1"
    )
    # Each invocation can bind the enrolled branch before dispatch. The raw
    # journal exists even though the request cannot publish a quality profile.
    report = json.loads(
        _native.optimize_record_provider_receipt(
            json.dumps(
                {"operation": "report", "batch_id": result["provider_receipt_batch_id"]}
            )
        )
    )
    assert report["status"] == "recorded"
    assert not report["summary"]["sealed"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("namespace", "openai"),
        ("sha256", "9" * 64),
        ("supported", False),
        ("version", "old"),
    ],
)
def test_foreign_or_malformed_endpoint_declaration_cannot_prepare_a_batch(field, value):
    source = call("https://openrouter.ai/api/v1", "openai/gpt-4o-mini")
    source["parameters"]["extra"]["agentc_route_context"]["provider_endpoint"][
        field
    ] = value
    assert prepare(source)["status"] == "abstained"


def test_router_catalog_matches_primary_observation_without_claiming_provider_pin():
    from pathlib import Path

    facts = json.loads(
        (
            Path(__file__).parents[1]
            / "bench/repro/openrouter-model-catalog-20261012.json"
        ).read_text()
    )
    catalog = agentc.model_catalog()
    for model in facts["models"]:
        target = next(
            row
            for row in catalog["targets"]
            if row["provider_namespace"] == "openrouter"
            and row["adapter_protocol"] == "openai.chat.completions.v1"
            and row["model_id"] == model["id"]
        )
        assert (
            target["revision_kind"] == "catalog_observation" and target["aliases"] == []
        )
        assert target["context_window_tokens"] == model["context_length"]
        assert (
            target["max_output_tokens"]
            == model["top_provider"]["max_completion_tokens"]
        )
        assert (
            target["price"]["input_per_million_tokens_usd"]
            == float(model["pricing"]["prompt"]) * 1e6
        )
        assert (
            target["price"]["output_per_million_tokens_usd"]
            == float(model["pricing"]["completion"]) * 1e6
        )
        assert target["price"]["source_url"] == facts["endpoint"]


SDK_WORKER = r"""
import json,os
from dataclasses import asdict
from pathlib import Path
import agentc,httpx,openai
root=Path(os.environ['AGENTC_STORAGE_PATH']);case=os.environ['ENDPOINT_CASE'];posts=[];gets=[]
actual_model='vendor/uncataloged-model' if case=='uncataloged' else 'openai/gpt-4o-mini'
provider='litellm' if case=='bridge' else 'openai'
logical_model=actual_model if provider=='openai' else 'openai/'+actual_model
responses={}
def handler(request):
    if request.method=='GET':
        generation=request.url.params['id'];gets.append(generation)
        assert generation in responses
        return httpx.Response(200,json={'data':{'id':generation,'request_id':'req-'+generation,'model':actual_model,'native_tokens_prompt':2,'native_tokens_completion':1,'total_cost':.01,'is_byok':False,'cancelled':False,'finish_reason':'stop'}})
    body=json.loads(request.content);posts.append(body)
    assert body['model']==actual_model and body.get('store') is False,body
    generation='gen-endpoint-'+str(len(posts));responses[generation]=True
    usage={'prompt_tokens':2,'completion_tokens':1,'total_tokens':3}
    if case!='uncataloged':usage.update({'cost':.01,'is_byok':False})
    return httpx.Response(200,headers={'x-request-id':'req-'+generation},json={'id':generation,'object':'chat.completion','created':0,'model':actual_model,'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':'original answer'}}],'usage':usage})
class OpaqueClient(httpx.Client):pass
class OpaqueAuth(httpx.Auth):
    def auth_flow(self,request):yield request
http_client=(OpaqueClient if case in {'transport','auth'} else httpx.Client)(transport=httpx.MockTransport(handler))
if case=='auth':http_client.auth=OpaqueAuth()
client=openai.OpenAI(api_key='PRIVATE_ENDPOINT_KEY',base_url='https://openrouter.ai/api/v1',max_retries=0,http_client=http_client)
agentc.init()
proof={'case':case,'batches':[],'receipts':[]}
try:
    with agentc.application_version('endpoint-sdk-v1'):
        requests=[agentc.CompletionRequest(provider,{'model':logical_model,'messages':[{'role':'user','content':'PRIVATE_ENDPOINT_PROMPT_'+str(i)}],'max_tokens':16},client) for i in range(2)]
        for mode in ['reference','guarded']:
            observations=[];schedules=[]
            output=agentc.completion_batch(requests,site='endpoint-sdk',policy=agentc.BatchPolicy(1000),mode=mode,on_observation=observations.append,on_schedule=schedules.append)
            assert len(output)==2 and all(row.choices[0].message.content=='original answer' for row in output)
            if case in {'uncataloged','transport','auth'}:
                assert observations[0].status=='abstained',observations
                if mode=='guarded':assert schedules[0].reason=='model_catalog_unqualified' and schedules[0].max_workers==1,schedules
            else:
                assert observations[0].status=='recorded',observations
            batch=observations[0].provider_receipt_batch_id;proof['batches'].append(batch)
            for receipt in agentc.provider_billing_receipts(batch):
                # Keep raw IDs transient; the original receipt contains only a
                # digest. This fixture matches each ID by its local response map.
                from agentc._provider_receipts import _provider_identity_fingerprint
                generation=next(value for value in responses if _provider_identity_fingerprint(value)==receipt.generation_sha256)
                result=agentc.reconcile_openrouter_generation(receipt,generation_id=generation,client=client)
                assert result.status==('abstained' if case in {'transport','auth'} else 'recorded'),result
                proof['receipts'].append(asdict(receipt))
    assert len(posts)==4 and len(gets)==(0 if case in {'transport','auth'} else 4),(posts,gets)
    proof['sdk_posts']=len(posts);proof['metadata_gets']=len(gets)
    (root/'endpoint-sdk-proof.json').write_text(json.dumps(proof))
finally:agentc.shutdown();client.close()
"""

SQL_WORKER = r"""
import json,sqlite3,sys
from pathlib import Path
assert not any(name=='agentc' or name.startswith('agentc.') for name in sys.modules)
root=Path(sys.argv[1]);proof=json.loads((root/'endpoint-sdk-proof.json').read_text());conn=sqlite3.connect(root/'cost_model.db')
assert conn.execute('SELECT COUNT(*) FROM provider_economic_batch').fetchone()[0]==2
assert conn.execute('SELECT COUNT(*) FROM provider_receipt_invocation').fetchone()[0]==proof['sdk_posts']==4
assert conn.execute('SELECT SUM(json_extract(completion_json,\'$.http_attempts\')) FROM provider_receipt_invocation').fetchone()[0]==(None if proof['case'] in {'transport','auth'} else 4)
assert conn.execute('SELECT COUNT(*) FROM provider_billing_attempt').fetchone()[0]==proof['metadata_gets']==(0 if proof['case'] in {'transport','auth'} else 4)
assert conn.execute('SELECT COUNT(*) FROM provider_billing_settlement').fetchone()[0]==(0 if proof['case'] in {'transport','auth'} else 4)
cohort=conn.execute('SELECT known_cost_usd,unknown_cost_invocations,pending_billing_verifications FROM provider_economic_cohort').fetchone()
assert abs(cohort[0]-(.04 if proof['case'] in {'transport','auth'} else .02))<1e-12 and cohort[1]==(4 if proof['case'] in {'transport','auth'} else 0) and cohort[2]==0,cohort
covered=conn.execute('SELECT COUNT(*) FROM provider_economic_publication').fetchone()[0]
assert covered==(1 if proof['case']=='bridge' else 0),covered
for row in conn.execute('SELECT invocation_json,completion_json FROM provider_receipt_invocation'):
    invocation=json.loads(row[0]);completion=json.loads(row[1])
    assert invocation['requested_model_id']==('vendor/uncataloged-model' if proof['case']=='uncataloged' else 'openai/gpt-4o-mini')
    assert completion['cost_source']==('estimated' if proof['case']=='uncataloged' else 'provider_reported')
    assert all(marker not in str(row) for marker in ['PRIVATE_ENDPOINT','https://','authorization','api_key'])
(root/'endpoint-sdk-sql-audit.json').write_text(json.dumps({'sdk_imported':False,'case':proof['case'],'sdk_posts':4,'metadata_gets':proof['metadata_gets'],'known_fixture_acquisition_cost_usd':cohort[0],'unknown_invocations':cohort[1],'pending_verifications':cohort[2],'covered_quality_executions':covered}))
"""


@pytest.mark.parametrize("case", ["uncataloged", "bridge", "transport", "auth"])
def test_actual_sdk_request_identity_is_owned_before_dispatch_and_billing(
    tmp_path, case
):
    import os
    import subprocess
    import sys

    env = {
        **os.environ,
        "AGENTC_STORAGE_PATH": str(tmp_path),
        "ENDPOINT_CASE": case,
        "AGENTC_OPTIMIZE": "1",
        "AGENTC_OPTIMIZE_EXPLORATION": "0",
        "AGENTC_OPTIMIZE_SHADOW": "0",
        "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": "1000",
        "AGENTC_CAPTURE_CONTENT": "0",
        "AGENTC_CAPTURE_EMBEDDINGS": "0",
    }
    for script, args in [(SDK_WORKER, []), (SQL_WORKER, [str(tmp_path)])]:
        result = subprocess.run(
            [sys.executable, "-I", "-c", script, *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
