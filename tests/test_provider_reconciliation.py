"""Authenticated billing overlays exercised through real SDK HTTP dispatch."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest


SDK_WORKER = r"""
import json,os
from dataclasses import asdict
from pathlib import Path
import agentc,httpx,openai
root=Path(os.environ["AGENTC_STORAGE_PATH"]);case=os.environ["BILLING_CASE"]
calls=[];gets=[];generation="gen-fixture-1";request_id="req-fixture-1"
def handler(request):
    if request.method=="GET":
        gets.append(1)
        assert request.url.path=="/api/v1/generation" and request.url.params["id"]==generation
        assert request.headers["authorization"]=="Bearer PRIVATE_BILLING_KEY"
        if case=="http_error":return httpx.Response(503,json={"error":"PRIVATE_METADATA_ERROR"})
        if case=="redirect":return httpx.Response(307,headers={"location":"https://foreign.invalid/generation"})
        data={"id":generation,"request_id":request_id,"model":"openai/gpt-4o-mini","native_tokens_prompt":2,"native_tokens_completion":1,"total_cost":.02,"is_byok":False,"cancelled":False,"finish_reason":"error" if case=="failed" else "stop","ignored_prompt":"PRIVATE_METADATA_PROMPT"}
        changes={"byok":{"is_byok":True},"foreign_generation":{"id":"gen-foreign"},"tokens":{"native_tokens_prompt":99},"model":{"model":"openai/foreign-model"},"request":{"request_id":"req-foreign"},"ongoing":{"finish_reason":None},"negative":{"total_cost":-1},"nonfinite":{"total_cost":float('inf')}}
        data.update(changes.get(case,{}))
        if case=="nonfinite":return httpx.Response(200,content=json.dumps({"data":data}),headers={"content-type":"application/json"})
        return httpx.Response(200,json={"data":data})
    calls.append(1)
    if case=="failed":
        return httpx.Response(500,headers={"x-request-id":request_id},json={"id":generation,"error":{"message":"PRIVATE_PROVIDER_ERROR","type":"server_error"}})
    return httpx.Response(200,headers={"x-request-id":request_id},json={"id":"missing" if case=="missing_generation" else generation,"object":"chat.completion","created":0,"model":"openai/gpt-4o-mini","choices":[{"index":0,"finish_reason":"stop","message":{"role":"assistant","content":"PRIVATE_BILLING_OUTPUT"}}],"usage":{"prompt_tokens":2,"completion_tokens":1,"total_tokens":3}})
client=openai.OpenAI(api_key="PRIVATE_BILLING_KEY",base_url="https://openrouter.ai/api/v1",max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(handler)))
agentc.init()
# This fixture deliberately has no retained batch-quality checkpoint. Bills
# alone must still leave scalar observations and quality evidence unchanged.
agentc._native.optimize_observe_multi_call=lambda _: '{"status":"abstained","reason":"billing-only fixture"}'
try:
    observations=[]
    with agentc.application_version("billing-fixture-v1"):
        try:
            responses=agentc.completion_batch([agentc.CompletionRequest("openai",{"model":"openai/gpt-4o-mini","messages":[{"role":"user","content":"PRIVATE_BILLING_PROMPT"}],"max_tokens":8},client)],site="billing",policy=agentc.BatchPolicy(1000),mode="reference",on_observation=observations.append)
            assert case!="failed" and isinstance(responses[0],openai.types.chat.ChatCompletion)
            assert responses[0].choices[0].message.content=="PRIVATE_BILLING_OUTPUT"
        except openai.InternalServerError as error:
            assert case=="failed" and error.request_id==request_id
    batch=observations[0].provider_receipt_batch_id
    receipts=agentc.provider_billing_receipts(batch);assert len(receipts)==1,receipts
    receipt=receipts[0]
    if os.environ.get("BILLING_ACQUIRE_ONLY")=="1":
        (root/"billing-proof.json").write_text(json.dumps({"case":"normal","batch":batch,"receipt":asdict(receipt),"sdk_sends":1,"metadata_gets":0}))
        raise SystemExit(0)
    original=agentc._native.optimize_record_provider_receipt
    if case=="persistence":
        def fail_finish(raw):
            if json.loads(raw)["operation"]=="finish_billing_lookup":return '{"status":"abstained"}'
            return original(raw)
        agentc._native.optimize_record_provider_receipt=fail_finish
    try:
        result=agentc.reconcile_openrouter_generation(receipt,generation_id=generation,client=client)
    finally:agentc._native.optimize_record_provider_receipt=original
    successful=case in {"normal","failed"}
    assert result.status==("recorded" if successful else "abstained"),(case,result)
    if successful:
        again=agentc.reconcile_openrouter_generation(receipt,generation_id=generation,client=client)
        assert again.status=="already_recorded" and again.cost_usd==.02,again
    assert len(calls)==1 and len(gets)==(0 if case=="missing_generation" else 1),(calls,gets)
    proof={"case":case,"batch":batch,"receipt":asdict(receipt),"result":asdict(result),"sdk_sends":len(calls),"metadata_gets":len(gets)}
    (root/"billing-proof.json").write_text(json.dumps(proof))
finally:agentc.shutdown();client.close()
"""

RESTART_WORKER = r"""
import json,os
from pathlib import Path
import agentc,httpx,openai
root=Path(os.environ["AGENTC_STORAGE_PATH"]);proof=json.loads((root/"billing-proof.json").read_text())
def forbidden(request):raise AssertionError("restart must not replay inference or billing lookup")
client=openai.OpenAI(api_key="fixture",base_url="https://openrouter.ai/api/v1",max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(forbidden)))
agentc.init()
try:
    receipt=agentc.ProviderBillingReceipt(**proof["receipt"])
    result=agentc.reconcile_openrouter_generation(receipt,generation_id="gen-fixture-1",client=client)
    assert result.status=="already_recorded" and result.cost_usd==.02,result
finally:agentc.shutdown();client.close()
"""

SQL_WORKER = r"""
import json,sqlite3,sys
from pathlib import Path
assert not any(name=="agentc" or name.startswith("agentc.") for name in sys.modules)
root=Path(sys.argv[1]);proof=json.loads((root/"billing-proof.json").read_text());case=proof["case"]
conn=sqlite3.connect(root/"cost_model.db")
raw=json.loads(conn.execute("SELECT completion_json FROM provider_receipt_invocation").fetchone()[0])
assert raw["http_attempts"]==1 and raw["status"]==("failed" if case=="failed" else "completed")
assert raw["cost_source"]==("unknown" if case=="failed" else "estimated")
assert raw.get("cost_usd")!=.02
assert len(raw["billing_scope_sha256"])==64 and len(raw["provider_generation_sha256"])==64
assert len(raw["provider_request_id_sha256"])==64
successful=case in {"normal","failed"}
cohort=conn.execute("SELECT known_cost_usd,unknown_cost_invocations,pending_billing_verifications,acquisition_latency_ms FROM provider_economic_cohort").fetchone()
assert abs(cohort[0]-(.02 if successful else 0))<1e-12 and cohort[1]==(0 if successful else 1),cohort
assert cohort[2]==(1 if case=="persistence" else 0),cohort
assert conn.execute("SELECT COUNT(*) FROM provider_billing_settlement").fetchone()[0]==int(successful)
assert conn.execute("SELECT COUNT(*) FROM provider_billing_attempt").fetchone()[0]==proof["metadata_gets"]
assert conn.execute("SELECT COUNT(*) FROM execution_plan_observation").fetchone()[0]==(0 if case=="failed" else 1)
assert conn.execute("SELECT COUNT(*) FROM execution_plan_observation WHERE cost_usd=.02").fetchone()[0]==0
assert conn.execute("SELECT COUNT(*) FROM provider_receipt_profile_publication WHERE result_json IS NOT NULL").fetchone()[0]==0
assert conn.execute("SELECT COUNT(*) FROM multi_call_comparison_spend").fetchone()[0]==0
for table in ["provider_receipt_invocation","provider_billing_attempt","provider_billing_settlement"]:
    for row in conn.execute("SELECT * FROM "+table):
        for marker in ["PRIVATE_BILLING","PRIVATE_PROVIDER_ERROR","PRIVATE_METADATA","gen-fixture-1","req-fixture-1","authorization","api_key"]:
            assert marker not in str(row),(table,marker)
(root/"billing-sql-audit.json").write_text(json.dumps({"sdk_imported":False,"cohort":cohort,"physical_sdk_sends":1,"metadata_gets":proof["metadata_gets"],"settled":successful,"original_cost_source":raw["cost_source"]}))
"""


def _run_worker(script, tmp_path, case, *args, extra_env=None, expected_exit=0):
    env = {
        **os.environ,
        "AGENTC_STORAGE_PATH": str(tmp_path),
        "BILLING_CASE": case,
        "AGENTC_OPTIMIZE": "1",
        "AGENTC_OPTIMIZE_EXPLORATION": "0",
        "AGENTC_OPTIMIZE_SHADOW": "0",
        "AGENTC_CAPTURE_CONTENT": "0",
        "AGENTC_CAPTURE_EMBEDDINGS": "0",
        "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": "1000",
        **(extra_env or {}),
    }
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == expected_exit, result.stderr


@pytest.mark.parametrize(
    "case",
    [
        "normal",
        "failed",
        "byok",
        "foreign_generation",
        "tokens",
        "model",
        "request",
        "ongoing",
        "negative",
        "nonfinite",
        "http_error",
        "redirect",
        "missing_generation",
        "persistence",
    ],
)
def test_verified_bill_resolves_only_its_receipt_and_preserves_failures(tmp_path, case):
    _run_worker(SDK_WORKER, tmp_path, case)
    if case in {"normal", "failed"}:
        _run_worker(RESTART_WORKER, tmp_path, case)
    _run_worker(SQL_WORKER, tmp_path, case, str(tmp_path))
    audit = json.loads((tmp_path / "billing-sql-audit.json").read_text())
    assert audit["sdk_imported"] is False


SCHEDULE_WORKER = r"""
import json,time,os
from dataclasses import asdict
from pathlib import Path
import agentc,httpx,openai
root=Path(os.environ["AGENTC_STORAGE_PATH"]);calls=[];gets=[];fail=False
failed_generation=None
def handler(request):
    global failed_generation
    if request.method=="GET":
        gets.append(1)
        assert request.url.params['id']==failed_generation
        return httpx.Response(200,json={'data':{'id':failed_generation,'request_id':'req-'+failed_generation,'model':'openai/gpt-4o-mini','native_tokens_prompt':0,'native_tokens_completion':0,'total_cost':.01,'is_byok':False,'cancelled':True,'finish_reason':None}})
    calls.append(1);time.sleep(.01);generation='gen-call-'+str(len(calls))
    if fail:
        failed_generation=generation
        return httpx.Response(500,headers={'x-request-id':'req-'+generation},json={'id':generation,'error':{'message':'fixture','type':'server_error'}})
    return httpx.Response(200,headers={'x-request-id':'req-'+generation},json={'id':generation,'object':'chat.completion','created':0,'model':'openai/gpt-4o-mini','choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':'answer'}}],'usage':{'prompt_tokens':2,'completion_tokens':1,'total_tokens':3,'cost':.01,'is_byok':False}})
client=openai.OpenAI(api_key='fixture',base_url='https://openrouter.ai/api/v1',max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(handler)))
requests=[agentc.CompletionRequest('openai',{'model':'openai/gpt-4o-mini','messages':[{'role':'user','content':'PRIVATE_SCHEDULE_PROMPT_'+str(i)}],'max_tokens':8},client) for i in range(2)]
agentc.init()
def run(mode):
    observations=[];schedules=[]
    try:
        responses=agentc.completion_batch(requests,site='billing-schedule',policy=agentc.BatchPolicy(100000),max_workers=2,mode=mode,on_observation=observations.append,on_schedule=schedules.append)
        assert all(response.choices[0].message.content=='answer' for response in responses)
    except openai.InternalServerError:
        assert fail
    return observations[0],schedules[0]
try:
    with agentc.application_version('billing-schedule-v1'):
        for i in range(20):
            reference,_=run('reference');candidate,_=run('candidate')
            assert agentc.compare_parallel_observations(reference,candidate).status=='recorded'
        _,before=run('guarded');assert before.max_workers==2,before
        fail=True;failed,_=run('reference');fail=False
        _,held=run('guarded');assert held.max_workers==1 and held.reason=='acquisition_costs_incomplete',held
        receipt=agentc.provider_billing_receipts(failed.provider_receipt_batch_id)[0]
        bill=agentc.reconcile_openrouter_generation(receipt,generation_id=failed_generation,client=client)
        assert bill.status=='recorded',bill
        _,resolved=run('guarded');assert resolved.max_workers==2 and resolved.acquisition_cost_complete,resolved
        assert abs(resolved.completed_acquisition_cost_usd-.81)<1e-12,resolved
        assert resolved.unknown_acquisition_invocations==0
        assert len(calls)==87 and len(gets)==1,(len(calls),len(gets))
        (root/'billing-schedule-proof.json').write_text(json.dumps({'before':asdict(before),'held':asdict(held),'resolved':asdict(resolved),'failed_batch':failed.provider_receipt_batch_id,'sdk_sends':len(calls),'metadata_gets':len(gets),'fixture_acquisition_cost_usd':.81}))
finally:agentc.shutdown();client.close()
"""

SCHEDULE_RESTART_WORKER = r"""
import json,os
from pathlib import Path
import agentc,httpx,openai
root=Path(os.environ["AGENTC_STORAGE_PATH"]);calls=[]
def handler(request):
    assert request.method=="POST";calls.append(1)
    return httpx.Response(200,json={"id":"gen-restart-"+str(len(calls)),"object":"chat.completion","created":0,"model":"openai/gpt-4o-mini","choices":[{"index":0,"finish_reason":"stop","message":{"role":"assistant","content":"answer"}}],"usage":{"prompt_tokens":2,"completion_tokens":1,"total_tokens":3,"cost":.01,"is_byok":False}})
client=openai.OpenAI(api_key="fixture",base_url="https://openrouter.ai/api/v1",max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(handler)))
requests=[agentc.CompletionRequest("openai",{"model":"openai/gpt-4o-mini","messages":[{"role":"user","content":"PRIVATE_SCHEDULE_PROMPT_"+str(i)}],"max_tokens":8},client) for i in range(2)]
agentc.init()
try:
    with agentc.application_version("billing-schedule-v1"):
        schedules=[]
        agentc.completion_batch(requests,site="billing-schedule",policy=agentc.BatchPolicy(100000),max_workers=2,on_schedule=schedules.append)
        assert schedules[0].max_workers==2 and schedules[0].acquisition_cost_complete,schedules
        assert abs(schedules[0].completed_acquisition_cost_usd-.81)<1e-12
        assert len(calls)==2
finally:agentc.shutdown();client.close()
"""

SCHEDULE_SQL_WORKER = r"""
import json,sqlite3,sys
from pathlib import Path
assert not any(name=="agentc" or name.startswith("agentc.") for name in sys.modules)
root=Path(sys.argv[1]);conn=sqlite3.connect(root/"cost_model.db")
proof=json.loads((root/"billing-schedule-proof.json").read_text())
assert conn.execute("SELECT COUNT(*) FROM provider_receipt_invocation").fetchone()[0]==proof["sdk_sends"]+2==89
assert conn.execute("SELECT SUM(json_extract(completion_json,'$.http_attempts')) FROM provider_receipt_invocation").fetchone()[0]==89
assert conn.execute("SELECT COUNT(*) FROM provider_billing_attempt").fetchone()[0]==proof["metadata_gets"]==1
assert conn.execute("SELECT COUNT(*) FROM provider_billing_settlement").fetchone()[0]==1
failed=proof["failed_batch"]
raw=json.loads(conn.execute("SELECT completion_json FROM provider_receipt_invocation WHERE batch_id=?",(failed,)).fetchone()[0])
assert raw["status"]=="failed" and raw["cost_source"]=="unknown" and raw["cost_usd"] is None
assert conn.execute("SELECT COUNT(*) FROM provider_receipt_profile_publication WHERE batch_id=?",(failed,)).fetchone()[0]==0
assert conn.execute("SELECT COUNT(*) FROM provider_economic_publication").fetchone()[0]==40
assert conn.execute("SELECT COUNT(*) FROM multi_call_comparison_pair").fetchone()[0]==20
cost,unknown,pending=conn.execute("SELECT known_cost_usd,unknown_cost_invocations,pending_billing_verifications FROM provider_economic_cohort").fetchone()
assert abs(cost-.81)<1e-12 and unknown==pending==0
(root/"billing-schedule-sql-audit.json").write_text(json.dumps({"sdk_imported":False,"sdk_sends":89,"metadata_gets":1,"quality_pairs":20,"covered_executions":40,"fixture_acquisition_cost_usd":cost,"unknown_invocations":unknown,"pending_verifications":pending}))
"""


def test_verified_failed_bill_unblocks_existing_schedule_across_restart(tmp_path):
    _run_worker(SCHEDULE_WORKER, tmp_path, "schedule")
    _run_worker(SCHEDULE_RESTART_WORKER, tmp_path, "schedule")
    _run_worker(SCHEDULE_SQL_WORKER, tmp_path, "schedule", str(tmp_path))


FAULT_SQL_WORKER = r"""
import json,sqlite3,sys
from pathlib import Path
assert not any(name=="agentc" or name.startswith("agentc.") for name in sys.modules)
root=Path(sys.argv[1]);fault=sys.argv[2];phase=sys.argv[3];conn=sqlite3.connect(root/"cost_model.db")
tables=["provider_receipt_invocation","execution_plan_observation","execution_plan_profile","multi_call_comparison_spend"]
if phase=="install":
    snapshot={table:list(conn.execute("SELECT * FROM "+table)) for table in tables}
    snapshot["foreground_latency_ms"]=conn.execute("SELECT acquisition_latency_ms FROM provider_economic_cohort").fetchone()[0]
    (root/"billing-before-sql.json").write_text(json.dumps(snapshot))
    targets={"settlement":"BEFORE INSERT ON provider_billing_settlement","invocation":"BEFORE UPDATE ON provider_economic_invocation","cohort":"BEFORE UPDATE ON provider_economic_cohort WHEN NEW.pending_billing_verifications < OLD.pending_billing_verifications","attempt":"BEFORE UPDATE OF result_json ON provider_billing_attempt WHEN NEW.result_json IS NOT NULL"}
    if fault!="postcommit":conn.execute("CREATE TRIGGER fail_billing "+targets[fault]+" BEGIN SELECT RAISE(ABORT,'billing fixture write failed'); END;")
    conn.commit()
else:
    prior=json.loads((root/"billing-before-sql.json").read_text())
    for table in tables:assert json.loads(json.dumps(list(conn.execute("SELECT * FROM "+table))))==prior[table],table
    result,input_json=conn.execute("SELECT result_json,result_input_json FROM provider_billing_attempt").fetchone()
    assert input_json is not None
    cost,unknown,pending,latency=conn.execute("SELECT known_cost_usd,unknown_cost_invocations,pending_billing_verifications,acquisition_latency_ms FROM provider_economic_cohort").fetchone()
    completed=phase=="after" or fault=="postcommit"
    assert (result is not None)==completed and cost==(.02 if completed else 0), (phase,cost,result)
    assert unknown==pending==(0 if completed else 1)
    assert conn.execute("SELECT COUNT(*) FROM provider_billing_settlement").fetchone()[0]==int(completed)
    if completed:assert abs(latency-prior["foreground_latency_ms"]-json.loads(result)["latency_ms"])<1e-8
    if phase=="pending" and fault!="postcommit":conn.execute("DROP TRIGGER fail_billing");conn.commit()
    if phase=="after":
        (root/"billing-recovery-sql-audit.json").write_text(json.dumps({"sdk_imported":False,"fault":fault,"sdk_sends":1,"metadata_gets":1,"known_fixture_cost_usd":cost,"pending_verifications":pending,"lookup_latency_ms":json.loads(result)["latency_ms"],"original_rows_unchanged":True}))
"""

FAULT_BILLING_WORKER = r"""
import json,os
from pathlib import Path
import agentc,httpx,openai
root=Path(os.environ["AGENTC_STORAGE_PATH"]);fault=os.environ["BILLING_CASE"]
proof=json.loads((root/"billing-proof.json").read_text());gets=[]
def handler(request):
    assert request.method=="GET" and request.url.path=="/api/v1/generation"
    gets.append(1);assert len(gets)==1
    proof["metadata_gets"]=1;(root/"billing-proof.json").write_text(json.dumps(proof))
    return httpx.Response(200,json={"data":{"id":"gen-fixture-1","request_id":"req-fixture-1","model":"openai/gpt-4o-mini","native_tokens_prompt":2,"native_tokens_completion":1,"total_cost":.02,"is_byok":False,"cancelled":False,"finish_reason":"stop"}})
client=openai.OpenAI(api_key="fixture",base_url="https://openrouter.ai/api/v1",max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(handler)))
agentc.init()
original=agentc._native.optimize_record_provider_receipt
if fault=="postcommit":
    def exit_after_commit(raw):
        result=original(raw)
        if json.loads(raw)["operation"]=="finish_billing_lookup" and json.loads(result).get("status")=="recorded":os._exit(79)
        return result
    agentc._native.optimize_record_provider_receipt=exit_after_commit
try:
    result=agentc.reconcile_openrouter_generation(agentc.ProviderBillingReceipt(**proof["receipt"]),generation_id="gen-fixture-1",client=client)
    assert fault!="postcommit" and result.status=="abstained",result
    assert len(gets)==1
finally:agentc.shutdown();client.close()
"""


@pytest.mark.parametrize(
    "fault", ["settlement", "invocation", "cohort", "attempt", "postcommit"]
)
def test_verified_billing_recovers_after_partial_write_without_another_lookup(
    tmp_path, fault
):
    _run_worker(SDK_WORKER, tmp_path, "normal", extra_env={"BILLING_ACQUIRE_ONLY": "1"})
    _run_worker(FAULT_SQL_WORKER, tmp_path, fault, str(tmp_path), fault, "install")
    _run_worker(
        FAULT_BILLING_WORKER,
        tmp_path,
        fault,
        expected_exit=79 if fault == "postcommit" else 0,
    )
    _run_worker(FAULT_SQL_WORKER, tmp_path, fault, str(tmp_path), fault, "pending")
    _run_worker(RESTART_WORKER, tmp_path, fault)
    _run_worker(RESTART_WORKER, tmp_path, fault)
    _run_worker(FAULT_SQL_WORKER, tmp_path, fault, str(tmp_path), fault, "after")
