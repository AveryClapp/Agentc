"""Realized model-and-rewrite graphs retain complete owned SDK evidence."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest


DISCOVERY_SDK = r"""
import agentc,openai,httpx,json,os,asyncio
mode=os.environ['AGENTC_GRAPH_DISCOVERY_MODE'];quantity=os.environ['AGENTC_GRAPH_DISCOVERY_QUANTITY'];rate=float(os.environ['AGENTC_GRAPH_DISCOVERY_RATE']);sends=[];callbacks=[]
strong='gpt-4o-2024-11-20'
def handler(request):
    body=json.loads(request.content);sends.append({'model':body['model'],'max_tokens':body['max_tokens']})
    usage={'prompt_tokens':2,'completion_tokens':1,'total_tokens':3,'prompt_tokens_details':{'cached_tokens':0}}
    if quantity=='billed_usd':usage.update(cost=.01 if body['model']==strong else .001,is_byok=False)
    return httpx.Response(200,json={'id':'fixture','object':'chat.completion','created':0,'model':body['model'],
        'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':'PRIVATE_DISCOVERY_RESULT'}}],'service_tier':'default','usage':usage})
client=(openai.AsyncOpenAI(api_key='fixture',max_retries=0,http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))) if mode=='async' else openai.OpenAI(api_key='fixture',max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(handler))))
agentc.init();scope=agentc.tariff_accounting() if quantity!='billed_usd' else None
if scope:scope.__enter__()
try:
    async def async_work(item):
        value=item
        for step in range(2):
            value=(await client.chat.completions.create(model=strong,messages=[{'role':'user','content':'PRIVATE_DISCOVERY_'+str(step)+'_'+value}],max_tokens=128)).choices[0].message.content
        return value
    def task(item):
        callbacks.append(item)
        if mode=='async':return asyncio.run(async_work(item))
        value=item
        for step in range(2):
            value=client.chat.completions.create(model=strong,messages=[{'role':'user','content':'PRIVATE_DISCOVERY_'+str(step)+'_'+value}],max_tokens=128).choices[0].message.content
        return value
    with agentc.application_version('discovery-sdk-v1'),agentc.optimization_scope('discovery-sdk',optimize=True):
        learning={'policy':{'sampling_rate':rate,'max_workflows':22,'max_samples':22,'max_physical_invocations':4,'replay_contract_sha256':'d'*64}}
        observed=[]
        for n in range(22):
            result=agentc.parallel_map(task,['INPUT_'+str(n)],comparison='graph',graph_sampling=learning,on_observation=observed.append)
            assert result==['PRIVATE_DISCOVERY_RESULT'] and observed[-1].graph_sampling,observed[-1]
            report=observed[-1].graph_sampling
            assert report['ticket']['ordinal']==n+1 and report['ticket']['sampled']==bool(rate)
            assert report['completion']['status']==('paired' if rate else 'reference_only'),report
        reports=[o.graph_sampling for o in observed]
        assert 'source_pair_id' not in reports[0]['ticket']['request'] and 'discovered_pair_id' not in reports[0]['ticket']
        if rate:
            assert all(r['ticket']['discovered_pair_id']==reports[0]['completion']['pair_id'] for r in reports[1:])
        else:assert all('discovered_pair_id' not in r['ticket'] for r in reports)
        assert len(sends)==(88 if rate else 44) and len(callbacks)==(44 if rate else 22)
        before_sends=len(sends);before_callbacks=len(callbacks)
        try:agentc.parallel_map(task,['EXHAUSTED'],comparison='graph',graph_sampling=learning)
        except RuntimeError:pass
        else:raise AssertionError('automatic discovery reset its lifetime cap')
        assert len(sends)==before_sends and len(callbacks)==before_callbacks
        costs=reports[-1]['acquisition'];assert costs['costs']['physical_invocations']==len(sends)
        assert any(s['model']!=strong or s['max_tokens']!=128 for s in sends)==bool(rate)
        proof={'mode':mode,'quantity':quantity,'rate':rate,'sdk_sends':len(sends),'callback_calls':len(callbacks),
            'reports':reports,'last_ticket_id':reports[-1]['ticket']['ticket_id']}
        assert 'PRIVATE_' not in json.dumps(proof);print(json.dumps(proof))
finally:
    if scope:scope.__exit__(None,None,None)
    agentc.shutdown()
    if mode=='async':asyncio.run(client.close())
    else:client.close()
"""


@pytest.mark.parametrize("quantity", ["billed_usd", "published_token_tariff_usd"])
@pytest.mark.parametrize("rate", ["0", "1"])
@pytest.mark.parametrize("mode", ["sync", "async"])
def test_cold_graph_discovery_sdk_creates_templates_without_seed_or_frozen_recipe(tmp_path, quantity, rate, mode):
    env = {**os.environ, "AGENTC_STORAGE_PATH": str(tmp_path), "AGENTC_OPTIMIZE": "1",
           "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": "1000", "AGENTC_OPTIMIZE_EXPLORATION": "1",
           "AGENTC_OPTIMIZE_SHADOW": "1", "AGENTC_CAPTURE_CONTENT": "0", "AGENTC_CAPTURE_EMBEDDINGS": "0",
           "AGENTC_GRAPH_DISCOVERY_QUANTITY": quantity, "AGENTC_GRAPH_DISCOVERY_RATE": rate, "AGENTC_GRAPH_DISCOVERY_MODE": mode}
    env.pop("AGENTC_EVAL_PLANNER_MODE", None)
    result = subprocess.run([sys.executable, "-I", "-c", DISCOVERY_SDK], env=env, capture_output=True, text=True, timeout=240)
    assert result.returncode == 0, result.stderr
    path = tmp_path / "provider-graph-discovery-sdk-proof.json"
    path.write_text(result.stdout)
    restart = subprocess.run([sys.executable, "-I", "-c", r"""
import agentc,json,sys
from pathlib import Path
p=json.loads(Path(sys.argv[1]).read_text());agentc.init()
try:
    r=json.loads(agentc._native.optimize_provider_graph_sampling(json.dumps({'operation':'finish','ticket_id':p['last_ticket_id']})))
    assert r['status']=='recorded' and r['sampling']==p['reports'][-1]['completion'],r
    driver=p['reports'][-1]['acquisition']['economics']['driver_id']
    r=json.loads(agentc._native.optimize_record_provider_receipt(json.dumps({'operation':'graph_acquisition_costs','driver_id':driver})))
    assert r['status']=='recorded' and r['acquisition']==p['reports'][-1]['acquisition'],r
finally:agentc.shutdown()
""", str(path)], env=env, capture_output=True, text=True, timeout=60)
    assert restart.returncode == 0, restart.stderr
    sql = r"""
import sqlite3,json,hashlib,sys
from pathlib import Path
assert not any(n=='agentc' or n.startswith('agentc.') for n in sys.modules)
p=Path(sys.argv[1]);proof=json.loads(p.read_text());selected=bool(proof['rate']);c=sqlite3.connect(p.parent/'cost_model.db')
assert c.execute('SELECT COUNT(*) FROM provider_receipt_invocation').fetchone()[0]==proof['sdk_sends']==(88 if selected else 44)
assert c.execute('SELECT COUNT(*) FROM provider_graph_execution').fetchone()[0]==(44 if selected else 22)
assert c.execute('SELECT COUNT(*) FROM provider_graph_pair').fetchone()[0]==(22 if selected else 0)
assert c.execute('SELECT COUNT(*) FROM provider_graph_quality_event').fetchone()[0]==(22 if selected else 0)
assert c.execute('SELECT workflows,samples FROM provider_graph_sampling_state').fetchone()==(22,22 if selected else 0)
assert c.execute('SELECT COUNT(*) FROM provider_graph_sampling_discovered').fetchone()[0]==int(selected)
assert c.execute('SELECT COUNT(*) FROM provider_graph_prepared_node').fetchone()[0]==(88 if selected else 44)
if selected:
    driver,ticket,pair=c.execute('SELECT driver_id,ticket_id,pair_id FROM provider_graph_sampling_discovered').fetchone()
    assert ticket==proof['reports'][0]['ticket']['ticket_id'] and pair==proof['reports'][0]['completion']['pair_id']
    assert c.execute('SELECT COUNT(*) FROM provider_graph_preparation WHERE json_extract(lease_json,"$.comparison_context") IS NOT NULL').fetchone()[0]==42
    candidate=json.loads(c.execute('SELECT execution_json FROM provider_graph_execution WHERE execution_id=(SELECT candidate_execution_id FROM provider_graph_pair WHERE pair_id=?)',(pair,)).fetchone()[0])
    assert any(b['plan']['target_model_id']!=b['plan']['requested_model_id'] or b['plan']['rewrites'] for b in candidate['spec']['calls']['branches'])
assert c.execute('SELECT COUNT(*) FROM provider_receipt_invocation WHERE json_extract(invocation_json,"$.role")!="primary"').fetchone()[0]==0
assert not c.execute('PRAGMA foreign_key_check').fetchall()
"""
    audit = subprocess.run([sys.executable, "-I", "-c", sql, str(path)], capture_output=True, text=True, timeout=30)
    assert audit.returncode == 0, audit.stderr

    if rate == "1":
        # Copy and mutate only after both SDK workers are terminal. The SQL and
        # native readers always remain in separate interpreter processes.
        corrupt_sql = r"""
import json,sqlite3,sys
from pathlib import Path
p=Path(sys.argv[1]);proof=json.loads(p.read_text());root=p.parent
for kind in ['cache','node']:
    directory=root/('corrupt_'+kind);directory.mkdir()
    src=sqlite3.connect(root/'cost_model.db');dst=sqlite3.connect(directory/'cost_model.db');src.backup(dst);src.close()
    if kind=='cache':dst.execute('DELETE FROM provider_graph_sampling_discovered')
    else:
        ref=proof['reports'][0]['ticket']['request']['reference_batch']['batch_id']
        dst.execute('DELETE FROM provider_graph_prepared_node WHERE lease_id=(SELECT lease_id FROM provider_graph_preparation WHERE batch_id=?)',(ref,))
    dst.commit();dst.close()
"""
        corrupt = subprocess.run([sys.executable, "-I", "-c", corrupt_sql, str(path)], capture_output=True, text=True, timeout=30)
        assert corrupt.returncode == 0, corrupt.stderr
        for kind in ["cache", "node"]:
            negative_env = {**env, "AGENTC_STORAGE_PATH": str(tmp_path / ("corrupt_" + kind))}
            negative = subprocess.run([sys.executable, "-I", "-c", r"""
import agentc,json,sys,uuid
from pathlib import Path
p=json.loads(Path(sys.argv[1]).read_text());kind=sys.argv[2];r=p['reports'][0]['ticket']['request']
r['reference_batch']['batch_id']=uuid.uuid4().hex;r['candidate_batch']['batch_id']=uuid.uuid4().hex
agentc.init()
try:
    value=json.loads(agentc._native.optimize_provider_graph_sampling(json.dumps({'operation':'begin','request':r})))
    assert value['status']=='abstained',value
    assert ('discovered graph template custody is missing' if kind=='cache' else 'discovery execution has incomplete issued node coverage') in value['reason'],value
finally:agentc.shutdown()
""", str(path), kind], env=negative_env, capture_output=True, text=True, timeout=60)
            assert negative.returncode == 0, negative.stderr


DEPLOYMENT_SAMPLING_SDK = r"""
import agentc,openai,httpx,json,os,asyncio
mode=os.environ['AGENTC_GRAPH_DEPLOYMENT_MODE'];quantity=os.environ['AGENTC_GRAPH_DEPLOYMENT_QUANTITY'];held=os.environ['AGENTC_GRAPH_DEPLOYMENT_HELD']=='1'
sends=[];callbacks=[];strong='gpt-4o-2024-11-20';cheap='gpt-4o-mini-2024-07-18'
def handler(request):
    body=json.loads(request.content);sends.append({'model':body['model'],'max_tokens':body['max_tokens']})
    content='PRIVATE_DEPLOYED_RESULT' if len(callbacks)>40 and body['model']==cheap else 'PRIVATE_REFERENCE_RESULT'
    usage={'prompt_tokens':2,'completion_tokens':1,'total_tokens':3,'prompt_tokens_details':{'cached_tokens':0}}
    if quantity=='billed_usd':usage.update(cost=.01 if body['model']==strong else .001,is_byok=False)
    return httpx.Response(200,json={'id':'fixture','object':'chat.completion','created':0,'model':body['model'],
        'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':content}}],'service_tier':'default','usage':usage})
client=(openai.AsyncOpenAI(api_key='fixture',max_retries=0,http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))) if mode=='async' else openai.OpenAI(api_key='fixture',max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(handler))))
agentc.init();scope=agentc.tariff_accounting() if quantity!='billed_usd' else None
if scope:scope.__enter__()
try:
    async def async_work(item):
        value=item
        for step in range(2):
            value=(await client.chat.completions.create(model=strong,messages=[{'role':'user','content':'PRIVATE_DEPLOY_SAMPLE_'+str(step)+'_'+value}],max_tokens=128)).choices[0].message.content
        return value
    def task(item):
        callbacks.append(item)
        if mode=='async':return asyncio.run(async_work(item))
        value=item
        for step in range(2):
            value=client.chat.completions.create(model=strong,messages=[{'role':'user','content':'PRIVATE_DEPLOY_SAMPLE_'+str(step)+'_'+value}],max_tokens=128).choices[0].message.content
        return value
    def costs(driver):
        r=json.loads(agentc._native.optimize_record_provider_receipt(json.dumps({'operation':'graph_acquisition_costs','driver_id':driver})))
        assert r['status']=='recorded',r
        return r['acquisition']
    quality={'divergence_threshold':0.,'exposure_budget':1.,'min_pairs':20}
    money={'quantity':quantity}
    if quantity!='billed_usd':money.update(snapshot_sha256=json.loads(agentc._native.optimize_record_provider_receipt(json.dumps({'operation':'tariff_policy'})))['snapshot_sha256'],max_expected_nanodollars=0 if held else None)
    admission={'remaining_batches':1000,'policy':{
        'selection':{'objective':'cost','min_paired_observations':20,'now_us':0,'max_profile_age_us':24*60*60*1000000,'divergence_exposure_budget':1.,'max_expected_cost_usd':0. if held and quantity=='billed_usd' else None,'max_expected_latency_ms':None},
        'quality':quality,'money':money,'max_workers':1,'max_physical_invocations':2}}
    with agentc.application_version('deployment-sampling-sdk-v1'),agentc.optimization_scope('deployment-sampling-sdk',optimize=True):
        learning={'policy':{'sampling_rate':1.,'max_workflows':1000,'max_samples':20,'max_physical_invocations':4,'max_lifetime_physical_invocations':90,'replay_contract_sha256':'f'*64},'deployment':admission}
        if os.environ.get('AGENTC_GRAPH_DEPLOYMENT_COMMITMENTS')=='1':
            commitment_money={'quantity':quantity}
            if quantity=='billed_usd':commitment_money.update(workflow_usd=.05,lifetime_usd=2.)
            else:commitment_money.update(snapshot_sha256=money['snapshot_sha256'],workflow_nanodollars=50000,lifetime_nanodollars=2000000)
            learning['policy']['commitments']={'money':commitment_money,'max_open_workflows':1,'max_pending_invocations':1}
        observed=[]
        for n in range(20):
            result=agentc.parallel_map(task,['TRAIN_'+str(n)],comparison='graph',graph_sampling=learning,on_observation=observed.append)
            assert result==['PRIVATE_REFERENCE_RESULT'] and observed[-1].graph_sampling['completion']['status']=='paired',observed[-1]
            assert observed[-1].graph_sampling['visible_role']=='reference'
        driver=observed[-1].graph_acquisition['driver_id'];before=costs(driver);assert len(sends)==80 and len(callbacks)==40
        for n in range(4):
            result=agentc.parallel_map(task,['DEPLOY_'+str(n)],comparison='graph',graph_sampling=learning,on_observation=observed.append)
            assert result==(['PRIVATE_REFERENCE_RESULT'] if held else ['PRIVATE_DEPLOYED_RESULT']),result
            report=observed[-1].graph_sampling
            assert report['visible_role']==('reference' if held else 'candidate') and not report['ticket']['sampled'],report
            assert report['completion']['status']==('reference_only' if held else 'deployed'),report
            if not held:
                assert report['ticket']['deployed'] and observed[-1].graph_decision and not observed[-1].graph_decision['selected_reference']
                assert observed[-1].graph_decision['request']['remaining_batches']<=980-n
        after=costs(driver);assert len(sends)==88 and len(callbacks)==44
        if 'commitments' in learning['policy']:
            commit=observed[-1].graph_sampling['commitments']
            assert commit['open_workflows']==commit['pending_invocations']==commit['held_workflows']==0 and not commit['budget_exceeded'],commit
            if quantity=='billed_usd':
                expected=sum(.01 if call['model']==strong else .001 for call in sends)
                assert expected<=commit['billed_committed_upper_usd']<expected+1e-12,commit
            else:
                expected=sum(15000 if call['model']==strong else 900 for call in sends)
                assert commit['tariff_committed_upper_nanodollars']==expected,commit
        if not held:
            assert after['costs']==before['costs'] and after['economics']==before['economics'],(before,after)
            assert after['deployment']['costs']['physical_invocations']==8 and len(after['deployment']['batches'])==4
        else:assert after['costs']['physical_invocations']==88 and not after.get('deployment',{}).get('batches')
        callbacks_before=len(callbacks)
        try:agentc.parallel_map(task,['EXHAUSTED'],comparison='graph',graph_sampling=learning)
        except RuntimeError as exc:assert 'lifetime physical allowance' in str(exc),exc
        else:raise AssertionError('deployment escaped its shared physical allowance')
        assert len(callbacks)==callbacks_before and len(sends)==88
        proof={'mode':mode,'quantity':quantity,'held':held,'sdk_sends':len(sends),'callback_calls':len(callbacks),
            'reports':[o.graph_sampling for o in observed],'decisions':[o.graph_decision for o in observed[20:]],
            'before':before,'after':after,'last_ticket_id':observed[-1].graph_sampling['ticket']['ticket_id']}
        assert 'PRIVATE_' not in json.dumps(proof);print(json.dumps(proof))
finally:
    if scope:scope.__exit__(None,None,None)
    agentc.shutdown()
    if mode=='async':asyncio.run(client.close())
    else:client.close()
"""


@pytest.mark.parametrize("quantity", ["billed_usd", "published_token_tariff_usd"])
@pytest.mark.parametrize("held", [False, True])
@pytest.mark.parametrize("mode", ["sync", "async"])
def test_graph_sampling_sdk_returns_admitted_candidates_and_keeps_joint_call_allowance(tmp_path, quantity, held, mode):
    env = {**os.environ, "AGENTC_STORAGE_PATH": str(tmp_path), "AGENTC_OPTIMIZE": "1",
           "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": "1000", "AGENTC_OPTIMIZE_EXPLORATION": "1",
           "AGENTC_OPTIMIZE_SHADOW": "1", "AGENTC_CAPTURE_CONTENT": "0", "AGENTC_CAPTURE_EMBEDDINGS": "0",
           "AGENTC_GRAPH_DEPLOYMENT_QUANTITY": quantity, "AGENTC_GRAPH_DEPLOYMENT_HELD": str(int(held)),
           "AGENTC_GRAPH_DEPLOYMENT_MODE": mode}
    env.pop("AGENTC_EVAL_PLANNER_MODE", None)
    result = subprocess.run([sys.executable, "-I", "-c", DEPLOYMENT_SAMPLING_SDK], env=env, capture_output=True, text=True, timeout=240)
    assert result.returncode == 0, result.stderr
    path = tmp_path / "provider-graph-deployment-sampling-sdk-proof.json"
    path.write_text(result.stdout)
    restart = subprocess.run([sys.executable, "-I", "-c", r"""
import agentc,json,sys,uuid
from pathlib import Path
p=json.loads(Path(sys.argv[1]).read_text());agentc.init()
try:
    r=json.loads(agentc._native.optimize_provider_graph_sampling(json.dumps({'operation':'finish','ticket_id':p['last_ticket_id']})))
    assert r['status']=='recorded' and r['sampling']==p['reports'][-1]['completion'],r
    request=p['reports'][-1]['ticket']['request'];request['reference_batch']['batch_id']=uuid.uuid4().hex;request['candidate_batch']['batch_id']=uuid.uuid4().hex
    r=json.loads(agentc._native.optimize_provider_graph_sampling(json.dumps({'operation':'begin','request':request})))
    assert r['status']=='abstained' and 'lifetime physical allowance' in r['reason'],r
    driver=p['after']['economics']['driver_id']
    r=json.loads(agentc._native.optimize_record_provider_receipt(json.dumps({'operation':'graph_acquisition_costs','driver_id':driver})))
    assert r['status']=='recorded' and r['acquisition']==p['after'],r
finally:agentc.shutdown()
""", str(path)], env=env, capture_output=True, text=True, timeout=60)
    assert restart.returncode == 0, restart.stderr
    audit = subprocess.run([sys.executable, "-I", "-c", r"""
import sqlite3,json,hashlib,sys
from pathlib import Path
assert not any(n=='agentc' or n.startswith('agentc.') for n in sys.modules)
p=Path(sys.argv[1]);proof=json.loads(p.read_text());held=proof['held'];c=sqlite3.connect(p.parent/'cost_model.db')
assert c.execute('SELECT workflows,samples FROM provider_graph_sampling_state').fetchone()==(24,20)
assert c.execute('SELECT COUNT(*) FROM provider_receipt_invocation').fetchone()[0]==proof['sdk_sends']==88
assert c.execute('SELECT COUNT(*) FROM provider_graph_sampling_invocation').fetchone()[0]==88
assert c.execute('SELECT COUNT(*) FROM provider_graph_pair').fetchone()[0]==20
assert c.execute('SELECT COUNT(*) FROM provider_graph_quality_event').fetchone()[0]==20
assert c.execute('SELECT COUNT(*) FROM provider_graph_execution').fetchone()[0]==44
assert c.execute('SELECT COUNT(*) FROM provider_graph_deployment').fetchone()[0]==(0 if held else 4)
assert c.execute('SELECT COUNT(*) FROM provider_graph_preparation WHERE json_extract(lease_json,"$.sampling_id") IS NOT NULL').fetchone()[0]==44
for lease,batch,hash,raw in c.execute('SELECT lease_id,batch_id,approval_sha256,approval_json FROM provider_graph_deployment'):
    assert hashlib.sha256(raw.encode()).hexdigest()==hash
    a=json.loads(raw);l=json.loads(c.execute('SELECT lease_json FROM provider_graph_preparation WHERE lease_id=?',(lease,)).fetchone()[0])
    assert l['sampling_id'] and l['deployment_id']==hash and a['horizon_cap']<=980
    t=json.loads(c.execute('SELECT ticket_json FROM provider_graph_sampling WHERE ticket_id=?',(l['sampling_id'],)).fetchone()[0])
    assert t['deployed'] and not t['sampled'] and t['request']['candidate_batch']['batch_id']==batch
assert not c.execute('PRAGMA foreign_key_check').fetchall()
""", str(path)], capture_output=True, text=True, timeout=30)
    assert audit.returncode == 0, audit.stderr


DEPLOYMENT_SAMPLING_FAILURE_SDK = DEPLOYMENT_SAMPLING_SDK.split("        for n in range(4):")[0].replace(
    "    content='PRIVATE_DEPLOYED_RESULT'", "    if len(sends)==82:return httpx.Response(500,json={'error':{'message':'fixture failure','type':'fixture'}})\n    content='PRIVATE_DEPLOYED_RESULT'",
) + r"""
        try:agentc.parallel_map(task,['FAIL_DEPLOY'],comparison='graph',graph_sampling=learning,on_observation=observed.append)
        except openai.InternalServerError:pass
        else:raise AssertionError('visible provider failure was swallowed or replayed')
        assert len(sends)==82 and len(callbacks)==41
        failure=observed[-1].graph_sampling
        assert failure['visible_role']=='candidate' and failure['visible_error_type']=='InternalServerError',failure
        assert failure['ticket']['deployed'] and failure['completion']['status']=='incomplete' and failure['completion']['pair_id'] is None,failure
        failed_costs=costs(driver);assert failed_costs['costs']['physical_invocations']==82
        assert failed_costs['costs']['billed_usd'] is None and failed_costs['economics']['unknown_cost_invocations']>=1
        result=agentc.parallel_map(task,['NEXT_VISIBLE'],comparison='graph',graph_sampling=learning,on_observation=observed.append)
        assert result==['PRIVATE_REFERENCE_RESULT'] and len(sends)==84 and len(callbacks)==42
        fallback=observed[-1].graph_sampling
        assert fallback['visible_role']=='reference' and fallback['completion']['status']=='reference_only',fallback
        final=costs(driver);assert final['costs']['physical_invocations']==84 and final['costs']['billed_usd'] is None
        proof={'mode':mode,'quantity':quantity,'sdk_sends':len(sends),'callback_calls':len(callbacks),
            'reports':[o.graph_sampling for o in observed],'failure':failure,'before':before,'after':final,
            'last_ticket_id':observed[-1].graph_sampling['ticket']['ticket_id']}
        assert 'PRIVATE_' not in json.dumps(proof);print(json.dumps(proof))
finally:
    if scope:scope.__exit__(None,None,None)
    agentc.shutdown()
    if mode=='async':asyncio.run(client.close())
    else:client.close()
"""


@pytest.mark.parametrize("quantity", ["billed_usd", "published_token_tariff_usd"])
@pytest.mark.parametrize("mode", ["sync", "async"])
def test_graph_sampling_sdk_visible_failure_retains_costs_and_does_not_replay_tools(tmp_path, quantity, mode):
    env = {**os.environ, "AGENTC_STORAGE_PATH": str(tmp_path), "AGENTC_OPTIMIZE": "1",
           "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": "1000", "AGENTC_OPTIMIZE_EXPLORATION": "1",
           "AGENTC_OPTIMIZE_SHADOW": "1", "AGENTC_CAPTURE_CONTENT": "0", "AGENTC_CAPTURE_EMBEDDINGS": "0",
           "AGENTC_GRAPH_DEPLOYMENT_QUANTITY": quantity, "AGENTC_GRAPH_DEPLOYMENT_HELD": "0",
           "AGENTC_GRAPH_DEPLOYMENT_MODE": mode}
    env.pop("AGENTC_EVAL_PLANNER_MODE", None)
    result = subprocess.run([sys.executable, "-I", "-c", DEPLOYMENT_SAMPLING_FAILURE_SDK], env=env, capture_output=True, text=True, timeout=240)
    assert result.returncode == 0, result.stderr
    path = tmp_path / "provider-graph-deployment-failure-sdk-proof.json"
    path.write_text(result.stdout)
    restart = subprocess.run([sys.executable, "-I", "-c", r"""
import agentc,json,sys
from pathlib import Path
p=json.loads(Path(sys.argv[1]).read_text());agentc.init()
try:
    for report in [p['failure'],p['reports'][-1]]:
        r=json.loads(agentc._native.optimize_provider_graph_sampling(json.dumps({'operation':'finish','ticket_id':report['ticket']['ticket_id']})))
        assert r['status']=='recorded' and r['sampling']==report['completion'],r
    driver=p['after']['economics']['driver_id']
    r=json.loads(agentc._native.optimize_record_provider_receipt(json.dumps({'operation':'graph_acquisition_costs','driver_id':driver})))
    assert r['status']=='recorded' and r['acquisition']==p['after'],r
finally:agentc.shutdown()
""", str(path)], env=env, capture_output=True, text=True, timeout=60)
    assert restart.returncode == 0, restart.stderr
    audit = subprocess.run([sys.executable, "-I", "-c", r"""
import sqlite3,json,sys
from pathlib import Path
assert not any(n=='agentc' or n.startswith('agentc.') for n in sys.modules)
p=Path(sys.argv[1]);proof=json.loads(p.read_text());c=sqlite3.connect(p.parent/'cost_model.db')
assert c.execute('SELECT workflows,samples FROM provider_graph_sampling_state').fetchone()==(22,20)
assert c.execute('SELECT COUNT(*) FROM provider_receipt_invocation').fetchone()[0]==proof['sdk_sends']==84
assert c.execute('SELECT COUNT(*) FROM provider_graph_sampling_invocation').fetchone()[0]==84
assert c.execute('SELECT COUNT(*) FROM provider_graph_pair').fetchone()[0]==20
assert c.execute('SELECT COUNT(*) FROM provider_graph_quality_event').fetchone()[0]==20
assert c.execute('SELECT COUNT(*) FROM provider_graph_execution').fetchone()[0]==41
assert c.execute('SELECT COUNT(*) FROM provider_graph_deployment').fetchone()[0]==1
assert c.execute('SELECT COUNT(*) FROM provider_receipt_invocation WHERE json_extract(completion_json,"$.status")="failed"').fetchone()[0]==1
assert not c.execute('PRAGMA foreign_key_check').fetchall()
""", str(path)], capture_output=True, text=True, timeout=30)
    assert audit.returncode == 0, audit.stderr


@pytest.mark.parametrize("quantity", ["billed_usd", "published_token_tariff_usd"])
def test_graph_sampling_sdk_commitments_include_admitted_deployment_costs(tmp_path, quantity, monkeypatch):
    monkeypatch.setenv("AGENTC_GRAPH_DEPLOYMENT_COMMITMENTS", "1")
    test_graph_sampling_sdk_returns_admitted_candidates_and_keeps_joint_call_allowance(tmp_path, quantity, False, "sync")


COMMITMENT_SAMPLING_SDK = r"""
import agentc,openai,httpx,json,os,asyncio
mode=os.environ['AGENTC_GRAPH_COMMIT_MODE'];quantity=os.environ['AGENTC_GRAPH_COMMIT_QUANTITY'];fault=os.environ['AGENTC_GRAPH_COMMIT_FAULT']=='1'
sends=[];callbacks=[];strong='gpt-4o-2024-11-20'
def handler(request):
    body=json.loads(request.content);sends.append(body['model'])
    if fault and len(sends)==4:return httpx.Response(500,json={'error':{'message':'fixture','type':'fixture'}})
    usage={'prompt_tokens':2,'completion_tokens':1,'total_tokens':3,'prompt_tokens_details':{'cached_tokens':0}}
    if quantity=='billed_usd':usage.update(cost=.01 if body['model']==strong else .001,is_byok=False)
    return httpx.Response(200,json={'id':'fixture','object':'chat.completion','created':0,'model':body['model'],
        'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':'PRIVATE_COMMIT_RESULT'}}],'service_tier':'default','usage':usage})
client=(openai.AsyncOpenAI(api_key='fixture',max_retries=0,http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))) if mode=='async' else openai.OpenAI(api_key='fixture',max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(handler))))
agentc.init();scope=agentc.tariff_accounting() if quantity!='billed_usd' else None
if scope:scope.__enter__()
try:
    async def async_work(item):
        value=item
        for step in range(2):
            value=(await client.chat.completions.create(model=strong,messages=[{'role':'user','content':'PRIVATE_COMMIT_'+str(step)+'_'+value}],max_tokens=128)).choices[0].message.content
        return value
    def task(item):
        callbacks.append(item)
        if mode=='async':return asyncio.run(async_work(item))
        value=item
        for step in range(2):
            value=client.chat.completions.create(model=strong,messages=[{'role':'user','content':'PRIVATE_COMMIT_'+str(step)+'_'+value}],max_tokens=128).choices[0].message.content
        return value
    money={'quantity':quantity}
    if quantity=='billed_usd':money.update(workflow_usd=.05,lifetime_usd=.12)
    else:money.update(snapshot_sha256=json.loads(agentc._native.optimize_record_provider_receipt(json.dumps({'operation':'tariff_policy'})))['snapshot_sha256'],workflow_nanodollars=50000,lifetime_nanodollars=120000)
    with agentc.application_version('commitment-sdk-v1'),agentc.optimization_scope('commitment-sdk',optimize=True):
        learning={'policy':{'sampling_rate':1.,'max_workflows':100,'max_samples':100,'max_physical_invocations':4,
            'replay_contract_sha256':'a'*64,'commitments':{'money':money,'max_open_workflows':1,'max_pending_invocations':1}}}
        observed=[];completed=0
        for n in range(12):
            before=(len(sends),len(callbacks))
            try:result=agentc.parallel_map(task,['INPUT_'+str(n)],comparison='graph',graph_sampling=learning,on_observation=observed.append)
            except RuntimeError as exc:
                assert 'commitment' in str(exc),exc
                assert before==(len(sends),len(callbacks))
                break
            assert result==['PRIVATE_COMMIT_RESULT'];completed+=1
            report=observed[-1].graph_sampling;commit=report['commitments']
            assert commit['open_workflows']==0 and commit['pending_invocations']==0 and not commit['budget_exceeded'],commit
            if fault and n==0:
                assert report['completion']['status']=='incomplete' and report['candidate_error_type']=='InternalServerError',report
                assert commit['held_workflows']==1 and commit['unknown_cost_workflows']==1,commit
            else:assert report['completion']['status']=='paired',report
        else:raise AssertionError('lifetime money commitment did not stop callbacks')
        assert completed>=1 and len(sends)==completed*4 and len(callbacks)==completed*2
        if not fault:assert observed[-1].graph_sampling['commitments']['held_workflows']==0
        final=observed[-1].graph_sampling['commitments'];changed=json.loads(json.dumps(learning))
        changed['policy']['commitments']['money']['lifetime_usd' if quantity=='billed_usd' else 'lifetime_nanodollars']*=2
        before=(len(sends),len(callbacks))
        try:agentc.parallel_map(task,['REPLENISH'],comparison='graph',graph_sampling=changed)
        except RuntimeError:pass
        else:raise AssertionError('changed commitment policy replenished its budget')
        assert before==(len(sends),len(callbacks))
        proof={'mode':mode,'quantity':quantity,'fault':fault,'sdk_sends':len(sends),'callback_calls':len(callbacks),
            'completed_workflows':completed,'reports':[o.graph_sampling for o in observed],
            'last_ticket_id':observed[-1].graph_sampling['ticket']['ticket_id'],'final':final}
        assert 'PRIVATE_' not in json.dumps(proof);print(json.dumps(proof))
finally:
    if scope:scope.__exit__(None,None,None)
    agentc.shutdown()
    if mode=='async':asyncio.run(client.close())
    else:client.close()
"""


@pytest.mark.parametrize("quantity", ["billed_usd", "published_token_tariff_usd"])
@pytest.mark.parametrize("fault", [False, True])
@pytest.mark.parametrize("mode", ["sync", "async"])
def test_graph_sampling_sdk_commitments_share_money_hold_failures_and_survive_restart(tmp_path, quantity, fault, mode):
    env = {**os.environ, "AGENTC_STORAGE_PATH": str(tmp_path), "AGENTC_OPTIMIZE": "1",
           "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": "1000", "AGENTC_OPTIMIZE_EXPLORATION": "1",
           "AGENTC_OPTIMIZE_SHADOW": "1", "AGENTC_CAPTURE_CONTENT": "0", "AGENTC_CAPTURE_EMBEDDINGS": "0",
           "AGENTC_GRAPH_COMMIT_QUANTITY": quantity, "AGENTC_GRAPH_COMMIT_FAULT": str(int(fault)), "AGENTC_GRAPH_COMMIT_MODE": mode}
    env.pop("AGENTC_EVAL_PLANNER_MODE", None)
    result = subprocess.run([sys.executable, "-I", "-c", COMMITMENT_SAMPLING_SDK], env=env, capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stderr
    path = tmp_path / "provider-graph-commitments-sdk-proof.json"
    path.write_text(result.stdout)
    restart = subprocess.run([sys.executable, "-I", "-c", r"""
import agentc,json,sys,uuid
from pathlib import Path
p=json.loads(Path(sys.argv[1]).read_text());agentc.init()
try:
    r=json.loads(agentc._native.optimize_provider_graph_sampling(json.dumps({'operation':'commitments','ticket_id':p['last_ticket_id']})))
    assert r['status']=='recorded' and r['sampling']['commitments']==p['final'],r
    request=p['reports'][-1]['ticket']['request'];request['reference_batch']['batch_id']=uuid.uuid4().hex;request['candidate_batch']['batch_id']=uuid.uuid4().hex
    r=json.loads(agentc._native.optimize_provider_graph_sampling(json.dumps({'operation':'begin','request':request})))
    assert r['status']=='abstained' and 'commitment' in r['reason'],r
finally:agentc.shutdown()
""", str(path)], env=env, capture_output=True, text=True, timeout=60)
    assert restart.returncode == 0, restart.stderr
    audit = subprocess.run([sys.executable, "-I", "-c", r"""
import sqlite3,json,hashlib,sys
from pathlib import Path
assert not any(n=='agentc' or n.startswith('agentc.') for n in sys.modules)
p=Path(sys.argv[1]);proof=json.loads(p.read_text());c=sqlite3.connect(p.parent/'cost_model.db')
assert c.execute('SELECT workflows,samples FROM provider_graph_sampling_state').fetchone()==(proof['completed_workflows'],proof['completed_workflows'])
assert c.execute('SELECT COUNT(*) FROM provider_receipt_invocation').fetchone()[0]==proof['sdk_sends']
assert c.execute('SELECT COUNT(*) FROM provider_graph_sampling_invocation').fetchone()[0]==proof['sdk_sends']
assert c.execute('SELECT COUNT(*) FROM provider_graph_pair').fetchone()[0]==proof['completed_workflows']-int(proof['fault'])
for h,raw in c.execute('SELECT invocation_sha256,r.invocation_json FROM provider_graph_sampling_invocation i JOIN provider_receipt_invocation r USING(invocation_id)'):
    assert hashlib.sha256(raw.encode()).hexdigest()==h
assert not c.execute('PRAGMA foreign_key_check').fetchall()
""", str(path)], capture_output=True, text=True, timeout=30)
    assert audit.returncode == 0, audit.stderr


LIFETIME_SAMPLING_SDK = r"""
import agentc,openai,httpx,json,os,asyncio
mode=os.environ['AGENTC_GRAPH_LIFETIME_MODE'];quantity=os.environ['AGENTC_GRAPH_LIFETIME_QUANTITY'];rate=float(os.environ['AGENTC_GRAPH_LIFETIME_RATE']);fail=os.environ['AGENTC_GRAPH_LIFETIME_FAIL']=='1'
sends=[];callbacks=[];strong='gpt-4o-2024-11-20'
def handler(request):
    body=json.loads(request.content);sends.append({'model':body['model'],'max_tokens':body['max_tokens']})
    if fail and len(callbacks)==4 and len(sends)==7:
        return httpx.Response(500,json={'error':{'message':'fixture failure','type':'fixture'}})
    usage={'prompt_tokens':2,'completion_tokens':1,'total_tokens':3,'prompt_tokens_details':{'cached_tokens':0}}
    if quantity=='billed_usd':usage.update(cost=.01 if body['model']==strong else .001,is_byok=False)
    return httpx.Response(200,json={'id':'fixture','object':'chat.completion','created':0,'model':body['model'],
        'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':'PRIVATE_LIFETIME_RESULT'}}],'service_tier':'default','usage':usage})
client=(openai.AsyncOpenAI(api_key='fixture',max_retries=0,http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))) if mode=='async' else openai.OpenAI(api_key='fixture',max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(handler))))
agentc.init();scope=agentc.tariff_accounting() if quantity!='billed_usd' else None
if scope:scope.__enter__()
try:
    async def async_work(item):
        value=item
        for step in range(2):
            value=(await client.chat.completions.create(model=strong,messages=[{'role':'user','content':'PRIVATE_LIFETIME_'+str(step)+'_'+value}],max_tokens=128)).choices[0].message.content
        return value
    def task(item):
        callbacks.append(item)
        if mode=='async':return asyncio.run(async_work(item))
        value=item
        for step in range(2):
            value=client.chat.completions.create(model=strong,messages=[{'role':'user','content':'PRIVATE_LIFETIME_'+str(step)+'_'+value}],max_tokens=128).choices[0].message.content
        return value
    with agentc.application_version('lifetime-sdk-v1'),agentc.optimization_scope('lifetime-sdk',optimize=True):
        learning={'policy':{'sampling_rate':rate,'max_workflows':22,'max_samples':22,'max_physical_invocations':4,'max_lifetime_physical_invocations':12,'replay_contract_sha256':'e'*64}}
        observed=[];workflows=3 if rate else 5
        for n in range(workflows):
            result=agentc.parallel_map(task,['INPUT_'+str(n)],comparison='graph',graph_sampling=learning,on_observation=observed.append)
            assert result==['PRIVATE_LIFETIME_RESULT'] and observed[-1].graph_sampling,observed[-1]
            report=observed[-1].graph_sampling
            assert report['ticket']['ordinal']==n+1 and report['ticket']['sampled']==bool(rate)
            assert report['completion']['status']==('incomplete' if fail and n==1 else 'paired' if rate else 'reference_only'),report
        expected_sends=(11 if fail else 12) if rate else 10
        assert len(sends)==expected_sends and len(callbacks)==(6 if rate else 5)
        before=(len(sends),len(callbacks))
        try:agentc.parallel_map(task,['EXHAUSTED'],comparison='graph',graph_sampling=learning)
        except RuntimeError as exc:assert 'lifetime physical allowance' in str(exc),exc
        else:raise AssertionError('exhausted shared lifetime allowance executed callbacks')
        assert before==(len(sends),len(callbacks))
        changed=json.loads(json.dumps(learning));changed['policy']['max_lifetime_physical_invocations']=24
        try:agentc.parallel_map(task,['REPLENISH'],comparison='graph',graph_sampling=changed)
        except RuntimeError:pass
        else:raise AssertionError('changed lifetime policy replenished the allowance')
        assert before==(len(sends),len(callbacks))
        reports=[o.graph_sampling for o in observed]
        costs=reports[-1]['acquisition'];assert costs['costs']['physical_invocations']==len(sends)
        if fail:
            assert reports[1]['candidate_error_type']=='InternalServerError',reports[1]
            assert costs['economics']['unknown_cost_invocations']>=1,costs
        proof={'mode':mode,'quantity':quantity,'rate':rate,'failed_candidate':fail,'sdk_sends':len(sends),
            'callback_calls':len(callbacks),'reports':reports,'last_ticket_id':reports[-1]['ticket']['ticket_id']}
        assert 'PRIVATE_' not in json.dumps(proof);print(json.dumps(proof))
finally:
    if scope:scope.__exit__(None,None,None)
    agentc.shutdown()
    if mode=='async':asyncio.run(client.close())
    else:client.close()
"""


@pytest.mark.parametrize("quantity", ["billed_usd", "published_token_tariff_usd"])
@pytest.mark.parametrize("rate,failed_candidate", [("0", False), ("1", False), ("1", True)])
@pytest.mark.parametrize("mode", ["sync", "async"])
def test_graph_sampling_sdk_lifetime_allowance_retains_failed_calls_and_survives_restart(tmp_path, quantity, rate, failed_candidate, mode):
    env = {**os.environ, "AGENTC_STORAGE_PATH": str(tmp_path), "AGENTC_OPTIMIZE": "1",
           "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": "1000", "AGENTC_OPTIMIZE_EXPLORATION": "1",
           "AGENTC_OPTIMIZE_SHADOW": "1", "AGENTC_CAPTURE_CONTENT": "0", "AGENTC_CAPTURE_EMBEDDINGS": "0",
           "AGENTC_GRAPH_LIFETIME_QUANTITY": quantity, "AGENTC_GRAPH_LIFETIME_RATE": rate,
           "AGENTC_GRAPH_LIFETIME_FAIL": str(int(failed_candidate)), "AGENTC_GRAPH_LIFETIME_MODE": mode}
    env.pop("AGENTC_EVAL_PLANNER_MODE", None)
    result = subprocess.run([sys.executable, "-I", "-c", LIFETIME_SAMPLING_SDK], env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    path = tmp_path / "provider-graph-lifetime-sdk-proof.json"
    path.write_text(result.stdout)
    restart = subprocess.run([sys.executable, "-I", "-c", r"""
import agentc,json,sys,uuid
from pathlib import Path
p=json.loads(Path(sys.argv[1]).read_text());agentc.init()
try:
    r=json.loads(agentc._native.optimize_provider_graph_sampling(json.dumps({'operation':'finish','ticket_id':p['last_ticket_id']})))
    assert r['status']=='recorded' and r['sampling']==p['reports'][-1]['completion'],r
    request=p['reports'][-1]['ticket']['request'];request['reference_batch']['batch_id']=uuid.uuid4().hex;request['candidate_batch']['batch_id']=uuid.uuid4().hex
    r=json.loads(agentc._native.optimize_provider_graph_sampling(json.dumps({'operation':'begin','request':request})))
    assert r['status']=='abstained' and 'lifetime physical allowance' in r['reason'],r
    driver=p['reports'][-1]['acquisition']['economics']['driver_id']
    r=json.loads(agentc._native.optimize_record_provider_receipt(json.dumps({'operation':'graph_acquisition_costs','driver_id':driver})))
    assert r['status']=='recorded' and r['acquisition']==p['reports'][-1]['acquisition'],r
finally:agentc.shutdown()
""", str(path)], env=env, capture_output=True, text=True, timeout=60)
    assert restart.returncode == 0, restart.stderr
    audit = subprocess.run([sys.executable, "-I", "-c", r"""
import sqlite3,json,hashlib,sys
from pathlib import Path
assert not any(n=='agentc' or n.startswith('agentc.') for n in sys.modules)
p=Path(sys.argv[1]);proof=json.loads(p.read_text());c=sqlite3.connect(p.parent/'cost_model.db')
selected=bool(proof['rate']);failed=proof['failed_candidate'];workflows=3 if selected else 5
assert c.execute('SELECT workflows,samples FROM provider_graph_sampling_state').fetchone()==(workflows,workflows if selected else 0)
assert c.execute('SELECT COUNT(*) FROM provider_receipt_invocation').fetchone()[0]==proof['sdk_sends']
assert c.execute('SELECT COUNT(*) FROM provider_graph_sampling_invocation').fetchone()[0]==proof['sdk_sends']
for id,ticket,batch,hash,raw in c.execute('SELECT i.invocation_id,i.ticket_id,i.batch_id,i.invocation_sha256,r.invocation_json FROM provider_graph_sampling_invocation i JOIN provider_receipt_invocation r USING(invocation_id)'):
    assert hash==hashlib.sha256(raw.encode()).hexdigest()
    t=json.loads(c.execute('SELECT ticket_json FROM provider_graph_sampling WHERE ticket_id=?',(ticket,)).fetchone()[0])
    assert batch in [t['request']['reference_batch']['batch_id'],t['request']['candidate_batch']['batch_id']]
    assert t['request']['policy']['max_lifetime_physical_invocations']==12
assert c.execute('SELECT COUNT(*) FROM provider_graph_pair').fetchone()[0]==((2 if failed else 3) if selected else 0)
assert c.execute('SELECT COUNT(*) FROM provider_receipt_invocation WHERE json_extract(completion_json,"$.status")="failed"').fetchone()[0]==int(failed)
assert not c.execute('PRAGMA foreign_key_check').fetchall()
""", str(path)], capture_output=True, text=True, timeout=30)
    assert audit.returncode == 0, audit.stderr


SAMPLING_SDK = r"""
import agentc,openai,httpx,json,os,random,asyncio
from agentc._evaluation_policy import FrozenEvaluationPolicy,evaluation_policy_scope
mode=os.environ['AGENTC_GRAPH_SAMPLING_MODE'];quantity=os.environ['AGENTC_GRAPH_SAMPLING_QUANTITY'];rate=float(os.environ['AGENTC_GRAPH_SAMPLING_RATE']);sends=[];callbacks=[];fail=False;cheap_in_failure=0
strong='gpt-4o-2024-11-20';cheap='gpt-4o-mini-2024-07-18'
def handler(request):
    global cheap_in_failure
    body=json.loads(request.content);sends.append(body['model'])
    if fail and body['model']==cheap:
        cheap_in_failure+=1
        if cheap_in_failure==2:return httpx.Response(500,json={'error':{'message':'fixture failure','type':'fixture'}})
    usage={'prompt_tokens':2,'completion_tokens':1,'total_tokens':3,'prompt_tokens_details':{'cached_tokens':0}}
    if quantity=='billed_usd':usage.update(cost=.01 if body['model']==strong else .001,is_byok=False)
    return httpx.Response(200,json={'id':'fixture','object':'chat.completion','created':0,'model':body['model'],
        'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':'PRIVATE_SAMPLED_RESULT'}}],'service_tier':'default','usage':usage})
client=(openai.AsyncOpenAI(api_key='fixture',max_retries=0,http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))) if mode=='async' else openai.OpenAI(api_key='fixture',max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(handler))))
policy=FrozenEvaluationPolicy.from_configuration(mode='best_static_joint',calibration_lock_sha256='a'*64,
    configuration={'strong_model':strong,'cheap_model':cheap,'divergence_threshold':.05,'static_plan':{'target_model':cheap,'rewrites':[{'rule':'OutputBudget','max_output_tokens':64}]},'router':None,'rewrite_by_target':{}})
agentc.init();scope=agentc.tariff_accounting() if quantity!='billed_usd' else None
if scope:scope.__enter__()
original_random=random.random
try:
    async def async_work(item):
        value=item
        for step in range(2):
            value=(await client.chat.completions.create(model=strong,messages=[{'role':'user','content':'PRIVATE_SAMPLE_'+str(step)+'_'+value}],max_tokens=128)).choices[0].message.content
        return value
    def task(item):
        callbacks.append(item)
        if mode=='async':return asyncio.run(async_work(item))
        value=item
        for step in range(2):
            value=client.chat.completions.create(model=strong,messages=[{'role':'user','content':'PRIVATE_SAMPLE_'+str(step)+'_'+value}],max_tokens=128).choices[0].message.content
        return value
    with agentc.application_version('sampling-sdk-v1'),evaluation_policy_scope(policy),agentc.optimization_scope('sampling-sdk',optimize=True):
        random.random=lambda:1.0
        originals=[];candidates=[]
        with agentc.optimization_scope('sampling-sdk',optimize=False):
            agentc.parallel_map(task,['SEED'],max_workers=1,comparison='graph',on_observation=originals.append)
        agentc.parallel_map(task,['SEED'],max_workers=1,comparison='graph',on_observation=candidates.append)
        source=json.loads(agentc._native.optimize_compare_provider_graph(originals[-1].graph_execution_id,candidates[-1].graph_execution_id))
        assert source['status']=='recorded' and source['quality']['lifetime_pairs']==1 and len(sends)==4,source
        random.random=lambda:0.0
        learning={'source_pair_id':source['pair_id'],'policy':{'sampling_rate':rate,'max_workflows':22,'max_samples':21,'max_physical_invocations':4,'replay_contract_sha256':'c'*64}}
        observed=[]
        for n in range(20):
            result=agentc.parallel_map(task,['LEARN_'+str(n)],comparison='graph',graph_sampling=learning,on_observation=observed.append)
            assert result==['PRIVATE_SAMPLED_RESULT'] and observed[-1].graph_sampling,observed[-1]
            report=observed[-1].graph_sampling
            assert report['ticket']['ordinal']==n+1 and report['ticket']['sampled']==bool(rate)
            assert report['completion']['status']==('paired' if rate else 'reference_only'),report
        assert len(sends)==(84 if rate else 44)
        assert len(callbacks)==(42 if rate else 22)
        before_error=len(sends);before_callbacks=len(callbacks)
        changed=json.loads(json.dumps(learning));changed['policy']['max_samples']=22
        try:agentc.parallel_map(task,['REPLENISH'],comparison='graph',graph_sampling=changed)
        except RuntimeError:pass
        else:raise AssertionError('changed lifetime sampling policy executed')
        assert len(sends)==before_error and len(callbacks)==before_callbacks
        fail=True
        result=agentc.parallel_map(task,['FAIL_CANDIDATE'],comparison='graph',graph_sampling=learning,on_observation=observed.append)
        assert result==['PRIVATE_SAMPLED_RESULT']
        failure=observed[-1].graph_sampling
        assert failure['completion']['status']==('incomplete' if rate else 'reference_only'),failure
        if rate:
            assert failure['candidate_error_type']=='InternalServerError'
            assert failure['completion']['pair_id'] is None
            assert failure['acquisition']['costs']['billed_usd'] is None
            if quantity=='billed_usd':
                assert failure['acquisition']['economics']['unknown_cost_invocations']==1
                assert failure['acquisition']['costs']['unpriced_usd_invocations']==1
            else:
                assert failure['acquisition']['economics']['unknown_cost_invocations']==len(sends)
                assert failure['acquisition']['costs']['published_token_tariff'] is None
                assert failure['acquisition']['costs']['unpriced_tariff_invocations']==1
        result=agentc.parallel_map(task,['CAP'],comparison='graph',graph_sampling=learning,on_observation=observed.append)
        assert result==['PRIVATE_SAMPLED_RESULT'] and not observed[-1].graph_sampling['ticket']['sampled']
        before_sends=len(sends);before_callbacks=len(callbacks)
        try:agentc.parallel_map(task,['EXHAUSTED'],comparison='graph',graph_sampling=learning)
        except RuntimeError:pass
        else:raise AssertionError('exhausted lifetime workflow limit executed')
        assert len(sends)==before_sends and len(callbacks)==before_callbacks
        assert len(sends)==(90 if rate else 48),len(sends)
        proof={'mode':mode,'quantity':quantity,'rate':rate,'sdk_sends':len(sends),'callback_calls':len(callbacks),'source':source,
            'reports':[o.graph_sampling for o in observed],'failure':failure,'last_ticket_id':observed[-1].graph_sampling['ticket']['ticket_id']}
        assert 'PRIVATE_' not in json.dumps(proof);print(json.dumps(proof))
finally:
    random.random=original_random
    if scope:scope.__exit__(None,None,None)
    agentc.shutdown()
    if mode=='async':asyncio.run(client.close())
    else:client.close()
"""


@pytest.mark.parametrize("quantity", ["billed_usd", "published_token_tariff_usd"])
@pytest.mark.parametrize("rate", ["0", "1"])
@pytest.mark.parametrize("mode", ["sync", "async"])
def test_prospective_graph_sampling_sdk_learns_and_retains_failed_candidate_costs(tmp_path, quantity, rate, mode):
    env = {**os.environ, "AGENTC_STORAGE_PATH": str(tmp_path), "AGENTC_OPTIMIZE": "1",
           "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": "1000", "AGENTC_OPTIMIZE_EXPLORATION": "0",
           "AGENTC_OPTIMIZE_SHADOW": "1", "AGENTC_CAPTURE_CONTENT": "0", "AGENTC_CAPTURE_EMBEDDINGS": "0",
           "AGENTC_GRAPH_SAMPLING_QUANTITY": quantity, "AGENTC_GRAPH_SAMPLING_RATE": rate, "AGENTC_GRAPH_SAMPLING_MODE": mode,
           "AGENTC_EVAL_PLANNER_MODE": "best_static_joint"}
    result = subprocess.run([sys.executable, "-I", "-c", SAMPLING_SDK], env=env, capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stderr
    path = tmp_path / "provider-graph-sampling-sdk-proof.json"
    path.write_text(result.stdout)
    restart = subprocess.run([sys.executable, "-I", "-c", r"""
import agentc,json,sys
from pathlib import Path
p=json.loads(Path(sys.argv[1]).read_text());agentc.init()
try:
    r=json.loads(agentc._native.optimize_provider_graph_sampling(json.dumps({'operation':'finish','ticket_id':p['last_ticket_id']})))
    assert r['status']=='recorded' and r['sampling']==p['reports'][-1]['completion'],r
    driver=p['reports'][-1]['acquisition']['economics']['driver_id']
    r=json.loads(agentc._native.optimize_record_provider_receipt(json.dumps({'operation':'graph_acquisition_costs','driver_id':driver})))
    assert r['status']=='recorded' and r['acquisition']==p['reports'][-1]['acquisition'],r
finally:agentc.shutdown()
""", str(path)], env=env, capture_output=True, text=True, timeout=60)
    assert restart.returncode == 0, restart.stderr
    sql = r"""
import sqlite3,json,hashlib,sys
from pathlib import Path
assert not any(n=='agentc' or n.startswith('agentc.') for n in sys.modules)
p=Path(sys.argv[1]);proof=json.loads(p.read_text());selected=bool(proof['rate']);c=sqlite3.connect(p.parent/'cost_model.db')
assert c.execute('SELECT COUNT(*) FROM provider_receipt_invocation').fetchone()[0]==proof['sdk_sends']==(90 if selected else 48)
assert c.execute('SELECT COUNT(*) FROM provider_graph_execution').fetchone()[0]==(44 if selected else 24)
assert c.execute('SELECT COUNT(*) FROM provider_graph_pair').fetchone()[0]==(21 if selected else 1)
assert c.execute('SELECT COUNT(*) FROM provider_graph_quality_event').fetchone()[0]==(21 if selected else 1)
assert c.execute('SELECT workflows,samples FROM provider_graph_sampling_state').fetchone()==(22,21 if selected else 0)
rows=c.execute('SELECT ticket_sha256,ticket_json,result_sha256,result_json FROM provider_graph_sampling ORDER BY ordinal').fetchall();assert len(rows)==22
for th,tr,rh,rr in rows:
    assert hashlib.sha256(tr.encode()).hexdigest()==th and hashlib.sha256(rr.encode()).hexdigest()==rh
    t=json.loads(tr);r=json.loads(rr);assert t['ticket_id']==r['ticket_id'] and t['sampled']==r['sampled']
assert c.execute('SELECT COUNT(*) FROM provider_graph_preparation WHERE json_extract(lease_json,"$.sampling_id") IS NOT NULL').fetchone()[0]==(43 if selected else 22)
assert c.execute('SELECT COUNT(*) FROM provider_receipt_invocation WHERE json_extract(invocation_json,"$.role")!="primary"').fetchone()[0]==0
assert proof['reports'][-1]['acquisition']['costs']['physical_invocations']==proof['sdk_sends']
assert not c.execute('PRAGMA foreign_key_check').fetchall()
"""
    audit = subprocess.run([sys.executable, "-I", "-c", sql, str(path)], capture_output=True, text=True, timeout=30)
    assert audit.returncode == 0, audit.stderr


AUTOMATIC_TEMPLATE_SDK = r"""
import agentc,openai,httpx,json,time,os,hashlib
from agentc._evaluation_policy import FrozenEvaluationPolicy,evaluation_policy_scope
quantity=os.environ['AGENTC_AUTOMATIC_GRAPH_QUANTITY'];recipe=os.environ['AGENTC_AUTOMATIC_GRAPH_RECIPE'];sends=[];callback_calls=[];cap=128
strong='gpt-4o-2024-11-20';cheap='gpt-4o-mini-2024-07-18'
def handler(request):
    body=json.loads(request.content);sends.append({'model':body['model'],'max_tokens':body['max_tokens']})
    usage={'prompt_tokens':2,'completion_tokens':1,'total_tokens':3,'prompt_tokens_details':{'cached_tokens':0}}
    if quantity=='billed_usd':usage.update(cost=.01 if body['model']==strong else .001,is_byok=False)
    return httpx.Response(200,json={'id':'fixture','object':'chat.completion','created':0,'model':body['model'],
        'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':'PRIVATE_AUTOMATIC_RESULT'}}],'service_tier':'default','usage':usage})
client=openai.OpenAI(api_key='fixture',max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(handler)))
rewrites=[] if recipe=='routing' else [{'rule':'OutputBudget','max_output_tokens':64}]
policy=FrozenEvaluationPolicy.from_configuration(mode='best_static_joint',calibration_lock_sha256='a'*64,
    configuration={'strong_model':strong,'cheap_model':cheap,'divergence_threshold':.05,'static_plan':{'target_model':cheap,'rewrites':rewrites},'router':None,'rewrite_by_target':{}})
agentc.init();scope=agentc.tariff_accounting() if quantity!='billed_usd' else None
if scope:scope.__enter__()
try:
    def task(item):
        callback_calls.append(item);value=item
        for step in range(2):
            value=client.chat.completions.create(model=strong,messages=[{'role':'user','content':'PRIVATE_AUTOMATIC_'+str(step)+'_'+value}],max_tokens=cap).choices[0].message.content
        return value
    def execute(item,reference=False,**kwargs):
        observed=[]
        with agentc.optimization_scope('automatic-template-sdk',optimize=not reference):
            values=agentc.parallel_map(task,[item],max_workers=1,comparison='graph',on_observation=observed.append,**kwargs)
        assert values==['PRIVATE_AUTOMATIC_RESULT'] and observed[-1].graph_execution_id,(observed[-1].status,observed[-1].reason)
        return observed[-1]
    def cost(driver):
        value=json.loads(agentc._native.optimize_record_provider_receipt(json.dumps({'operation':'graph_acquisition_costs','driver_id':driver})))
        assert value['status']=='recorded',value
        return value['acquisition']
    with agentc.application_version('automatic-template-sdk-v1'),evaluation_policy_scope(policy):
        for _ in range(20):
            r=execute('TRAIN',True);c=execute('TRAIN')
            source=json.loads(agentc._native.optimize_compare_provider_graph(r.graph_execution_id,c.graph_execution_id));assert source['status']=='recorded',source
        assert len(sends)==80 and source['quality']['lifetime_pairs']==20
        # Neither callback constructs nor consumes a Plan. The SDK materializes
        # both original and candidate templates from the original pair.
        r=execute('NEW',True,graph_template_id=r.graph_execution_id,graph_pair_id=source['pair_id'])
        c=execute('NEW',graph_template_id=c.graph_execution_id,graph_pair_id=source['pair_id'])
        source=json.loads(agentc._native.optimize_compare_provider_graph(r.graph_execution_id,c.graph_execution_id));assert source['status']=='recorded',source
        assert len(sends)==84 and source['quality']['lifetime_pairs']==21
        original=json.loads(agentc._native.optimize_record_provider_receipt(json.dumps({'operation':'graph_execution_costs','kind':'primary','execution_id':c.graph_execution_id})))['projection']
        driver=c.graph_acquisition['driver_id'];before=cost(driver);money={'quantity':quantity}
        if quantity!='billed_usd':money.update(snapshot_sha256=before['costs']['published_token_tariff']['snapshot_sha256'],max_expected_nanodollars=None)
        request={'kind':'primary','source_pair_ids':[source['pair_id']],'window_size':source['quality']['window_size'],'remaining_batches':1000,'policy':{
            'selection':{'objective':'cost','min_paired_observations':20,'now_us':0,'max_profile_age_us':24*60*60*1000000,'divergence_exposure_budget':source['quality']['policy']['exposure_budget'],'max_expected_cost_usd':None,'max_expected_latency_ms':None},
            'quality':source['quality']['policy'],'money':money,'max_workers':1,'max_physical_invocations':2}}
        d=execute('DEPLOY',graph_template_id=c.graph_execution_id,graph_admission=request)
        assert d.graph_decision and not d.graph_decision['selected_reference'] and len(sends)==86
        after=cost(driver);assert after['costs']==before['costs'] and after['economics']==before['economics']
        assert after['deployment']['batches']==[d.provider_receipt_batch_id] and after['deployment']['costs']['physical_invocations']==2
        before_sends=len(sends);before_callbacks=len(callback_calls)
        held_request=json.loads(json.dumps(request));held_request['policy']['max_physical_invocations']=1
        try:execute('NOT_STARTED',graph_template_id=c.graph_execution_id,graph_admission=held_request)
        except RuntimeError:pass
        else:raise AssertionError('held admission executed its callback')
        assert len(sends)==before_sends and len(callback_calls)==before_callbacks
        cap=256
        try:execute('CHANGED',graph_template_id=c.graph_execution_id,graph_admission=request)
        except RuntimeError:pass
        else:raise AssertionError('changed automatic source controls reached HTTP')
        assert len(sends)==86
        expected_cap=128 if recipe=='routing' else 64
        assert all(s['max_tokens']==(128 if s['model']==strong else expected_cap) for s in sends)
        final=cost(driver);assert final['economics']['persistence_failed_batches']==1
        proof={'quantity':quantity,'recipe':recipe,'sdk_sends':len(sends),'callback_calls':len(callback_calls),'source':source,
            'original':original,'before':before,'after':after,'final':final,'deployment_execution_id':d.graph_execution_id,
            'deployment_batch':d.provider_receipt_batch_id,'decision':d.graph_decision,'expected_candidate_output_cap':expected_cap}
        assert 'PRIVATE_' not in json.dumps(proof);print(json.dumps(proof))
finally:
    if scope:scope.__exit__(None,None,None)
    agentc.shutdown();client.close()
"""


@pytest.mark.parametrize("quantity", ["billed_usd", "published_token_tariff_usd"])
@pytest.mark.parametrize("recipe", ["routing", "joint"])
def test_automatic_graph_template_sdk_materializes_calls_and_admits_before_callback(tmp_path, quantity, recipe):
    env = {
        **os.environ, "AGENTC_STORAGE_PATH": str(tmp_path), "AGENTC_OPTIMIZE": "1",
        "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": "1000", "AGENTC_OPTIMIZE_EXPLORATION": "0",
        "AGENTC_OPTIMIZE_SHADOW": "0", "AGENTC_CAPTURE_CONTENT": "0", "AGENTC_CAPTURE_EMBEDDINGS": "0",
        "AGENTC_AUTOMATIC_GRAPH_QUANTITY": quantity, "AGENTC_AUTOMATIC_GRAPH_RECIPE": recipe,
        "AGENTC_EVAL_PLANNER_MODE": "best_static_joint",
    }
    result = subprocess.run([sys.executable, "-I", "-c", AUTOMATIC_TEMPLATE_SDK], env=env, capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stderr
    path = tmp_path / "provider-graph-automatic-template-sdk-proof.json"
    path.write_text(result.stdout)
    restart = subprocess.run([sys.executable, "-I", "-c", r"""
import agentc,json,sys
from pathlib import Path
p=json.loads(Path(sys.argv[1]).read_text());agentc.init()
try:
    value=json.loads(agentc._native.optimize_record_provider_receipt(json.dumps({'operation':'graph_acquisition_costs','driver_id':p['final']['economics']['driver_id']})))
    assert value['status']=='recorded' and value['acquisition']==p['final'],value
    value=json.loads(agentc._native.optimize_record_provider_receipt(json.dumps({'operation':'graph_execution_costs','kind':'primary','execution_id':p['deployment_execution_id']})))
    assert value['status']=='recorded' and value['projection']['key']==p['decision']['selected_key'],value
finally:agentc.shutdown()
""", str(path)], env=env, capture_output=True, text=True, timeout=60)
    assert restart.returncode == 0, restart.stderr
    sql = r"""
import json,sqlite3,hashlib,sys
from pathlib import Path
assert not any(n=='agentc' or n.startswith('agentc.') for n in sys.modules)
p=Path(sys.argv[1]);proof=json.loads(p.read_text());c=sqlite3.connect(p.parent/'cost_model.db')
assert proof['sdk_sends']==c.execute('SELECT COUNT(*) FROM provider_receipt_invocation').fetchone()[0]==86
assert c.execute('SELECT COUNT(*) FROM provider_graph_execution').fetchone()[0]==43
assert c.execute('SELECT COUNT(*) FROM provider_graph_pair').fetchone()[0]==21
assert c.execute('SELECT COUNT(*) FROM provider_graph_quality_event').fetchone()[0]==21
assert c.execute('SELECT COUNT(*) FROM provider_graph_deployment').fetchone()[0]==2
assert c.execute('SELECT COUNT(*) FROM provider_graph_deployment_node').fetchone()[0]==2
for hash_,raw in c.execute('SELECT binding_sha256,binding_json FROM provider_graph_deployment_node'):
    assert hashlib.sha256(raw.encode()).hexdigest()==hash_
    binding=json.loads(raw);assert binding['node']['plan']['target_model_id']=='gpt-4o-mini-2024-07-18'
    assert binding['node']['plan']['output_budget']==proof['expected_candidate_output_cap']
assert proof['after']['costs']==proof['before']['costs'] and proof['final']['deployment']['costs']['physical_invocations']==2
assert not c.execute('PRAGMA foreign_key_check').fetchall()
"""
    audit = subprocess.run([sys.executable, "-I", "-c", sql, str(path)], capture_output=True, text=True, timeout=30)
    assert audit.returncode == 0, audit.stderr


TYPED_VALIDATION_SDK = r"""
import json,time,os,hashlib
import agentc,openai,httpx
from agentc._optimizer import plan_call
from agentc._patches._optimizer_glue import build_call_dict_openai
from agentc._provider_receipts import ProviderReceiptBatch,instrument_provider,bind_plan,note_logical_call,_request_fingerprint,_transport_fingerprint
from agentc._dispatch_context import provider_dispatch
from agentc._batch_observation import comparison_fingerprint,GRAPH_COMPARISON_VERSION
case=os.environ['AGENTC_TYPED_VERDICT_CASE'];quantity=os.environ['AGENTC_TYPED_VERDICT_QUANTITY'];sends=[]
cheap='gpt-4o-mini-2024-07-18';strong='gpt-4o-2024-11-20';protocol='openai.chat.completions.v1'
answers={'accepted':'{"accepted":true}','rejected':'{"accepted":false}',
    'duplicate':'{"accepted":true,"accepted":false}','string':'{"accepted":"true"}',
    'extra':'{"accepted":true,"confidence":1}','non_json':'accept','multiple_choices':'{"accepted":true}'}
def handler(request):
    body=json.loads(request.content);name=body['messages'][0]['content'];sends.append(name)
    content=answers[case] if name=='PRIVATE_TYPED_VALIDATION' else 'PRIVATE_TYPED_'+('SOURCE' if name=='PRIVATE_TYPED_FALLBACK' else 'RESULT')
    choices=[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':content}}]
    if name=='PRIVATE_TYPED_VALIDATION' and case=='multiple_choices':choices.append({'index':1,'finish_reason':'stop','message':{'role':'assistant','content':'{"accepted":false}'}})
    usage={'prompt_tokens':2,'completion_tokens':1,'total_tokens':3,'prompt_tokens_details':{'cached_tokens':0}}
    if quantity=='billed_usd':usage.update(cost=.01,is_byok=False)
    return httpx.Response(200,json={'id':'fixture','object':'chat.completion','created':0,'model':body['model'],
        'choices':choices,'service_tier':'default','usage':usage})
client=openai.OpenAI(api_key='fixture',max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(handler)))
agentc.init();scope=agentc.tariff_accounting() if quantity!='billed_usd' else None
if scope:scope.__enter__()
try:
    plans={};nodes={};bodies={}
    with agentc.application_version('typed-validation-sdk-v1'):
        for name,model,logical in [('primary',cheap,0),('validation',strong,0),('fallback',strong,0),('consumer',cheap,1)]:
            body={'model':model,'messages':[{'role':'user','content':'PRIVATE_TYPED_'+name.upper()}],'max_tokens':8};bodies[name]=body
            source=build_call_dict_openai(body,call_site_id='typed-'+name,trace_id_hex='0'*32,span_id_hex=format(len(plans)+1,'016x'),client=client)
            plan=plan_call(source,observation_only=True);plans[name]=plan
            value=json.loads(agentc._native.optimize_describe_provider_graph_node(json.dumps({'item':0,'call':logical}),plan.source_call_json,plan.raw_json))
            assert value['status']=='recorded',value
            nodes[name]=value['node']
    graph={'version':'provider-call-graph-v1','calls':{'application_version':json.loads(plans['primary'].source_call_json)['parameters']['extra']['agentc_route_context']['application_version']['sha256'],
        'callback_version':'typed-validation-sdk-v1','item_count':1,'max_workers':1,'branches':[
            {'position':nodes[name]['position'],'call_site_version':nodes[name]['key']['call_site_version'],'plan':nodes[name]['plan']} for name in ['primary','consumer']]},
        'dependencies':[{'producer':{'item':0,'call':0},'consumer':{'item':0,'call':1}}],'results':{'kind':'callback_items','items':[0]}}
    primary_slot={'position':nodes['primary']['position'],'ordinal':0};validator_slot={'position':nodes['primary']['position'],'ordinal':1}
    contract=hashlib.sha256(b'strict-accepted-boolean-v1').hexdigest();dispatches=[];bindings=[]
    for ordinal,name in enumerate(['primary','validation','fallback','consumer']):
        slot={'position':nodes[name]['position'],'ordinal':0 if name=='consumer' else ordinal}
        activation={'kind':'validate','subject':primary_slot,'contract_sha256':contract} if name=='validation' else {'kind':'on_validation_rejection','predecessor':primary_slot,'validator':validator_slot} if name=='fallback' else {'kind':'always'}
        dispatches.append({'slot':slot,'role':'primary' if name=='consumer' else name,'call_site_version':nodes[name]['key']['call_site_version'],'plan':nodes[name]['plan'],'activation':activation})
        bindings.append({'slot':slot,'runtime':nodes[name]['runtime'],'request_sha256':_request_fingerprint(bodies[name],protocol),'transport_sha256':_transport_fingerprint(client.chat.completions,protocol)})
    prepared={'program':{'version':'provider-role-graph-v1','graph':graph,'dispatches':dispatches},'bindings':bindings,
        'logical_requests':[{'position':nodes[name]['position'],'sha256':nodes[name]['logical_request_sha256']} for name in ['primary','consumer']]}
    input_hash=comparison_fingerprint([0],version=GRAPH_COMPARISON_VERSION)
    b=ProviderReceiptBatch(graph['calls']['application_version'],'typed-validation-sdk-v1',1);b.prepare_role_graph(prepared,input_hash)
    def invoke(name):
        owner=plans['consumer' if name=='consumer' else 'primary']
        with b.item(0):
            note_logical_call(1 if name=='consumer' else 0);bind_plan(owner)
            provider_call=instrument_provider(client.chat.completions.create.__wrapped__,client.chat.completions,protocol)
            with provider_dispatch('primary' if name=='consumer' else name,plan=owner,primary_plan=owner,dispatch_plan=plans[name]):
                return provider_call(**bodies[name])
    start=time.perf_counter();invoke('primary');invoke('validation');e=None
    if case=='rejected':invoke('fallback')
    if case in ['accepted','rejected']:result=invoke('consumer').choices[0].message.content
    else:
        before=len(sends)
        try:invoke('consumer')
        except RuntimeError:pass
        else:raise AssertionError('ambiguous verdict released its consumer')
        assert len(sends)==before==2 and b.failed
        result='PRIVATE_WITHHELD'
    b.seal([2]);b.finish_economics((time.perf_counter()-start)*1000)
    command={'operation':'finish_role_graph','lease_id':b.graph_preparation_id,'comparison':{'version':GRAPH_COMPARISON_VERSION,'input_sha256':input_hash,'output_sha256':comparison_fingerprint([result],version=GRAPH_COMPARISON_VERSION)}}
    value=json.loads(agentc._native.optimize_record_provider_receipt(json.dumps(command)))
    if case in ['accepted','rejected']:
        assert value['status']=='recorded',value
        e=value['execution'];assert e['validation_sha256'] and e['selected_results'][0]['slot']['ordinal']==(2 if case=='rejected' else 0)
        assert json.loads(agentc._native.optimize_record_provider_receipt(json.dumps(command)))['execution']==e
    else:assert value['status']=='abstained',value
    expected=4 if case=='rejected' else 3 if case=='accepted' else 2
    assert len(sends)==expected
    acquisition=b.acquisition_report();assert acquisition['physical_invocations']==expected
    assert acquisition['cost_complete']==(case in ['accepted','rejected'] and quantity=='billed_usd')
    proof={'case':case,'quantity':quantity,'sdk_sends':len(sends),'lease_id':b.graph_preparation_id,'batch_id':b.batch_id,'contract_sha256':contract,'execution':e,'acquisition':acquisition}
    assert 'PRIVATE_' not in json.dumps(proof);print(json.dumps(proof))
finally:
    if scope:scope.__exit__(None,None,None)
    agentc.shutdown();client.close()
"""


@pytest.mark.parametrize("case", ["accepted", "rejected", "duplicate", "string", "extra", "non_json", "multiple_choices"])
@pytest.mark.parametrize("quantity", ["billed_usd", "published_token_tariff_usd"])
def test_typed_graph_validation_sdk_selects_accepted_result_or_holds_ambiguous_output(tmp_path, case, quantity):
    env = {
        **os.environ, "AGENTC_STORAGE_PATH": str(tmp_path), "AGENTC_OPTIMIZE": "1",
        "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": "1000", "AGENTC_OPTIMIZE_EXPLORATION": "0",
        "AGENTC_OPTIMIZE_SHADOW": "0", "AGENTC_CAPTURE_CONTENT": "0", "AGENTC_CAPTURE_EMBEDDINGS": "0",
        "AGENTC_TYPED_VERDICT_CASE": case, "AGENTC_TYPED_VERDICT_QUANTITY": quantity,
    }
    result = subprocess.run([sys.executable, "-I", "-c", TYPED_VALIDATION_SDK], env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    path = tmp_path / "provider-graph-typed-validation-sdk-proof.json"
    path.write_text(result.stdout)
    restart = subprocess.run([sys.executable, "-I", "-c", r"""
import agentc,json,sys
from pathlib import Path
p=json.loads(Path(sys.argv[1]).read_text());agentc.init()
try:
    if p['execution']:
        value=json.loads(agentc._native.optimize_record_provider_receipt(json.dumps({'operation':'role_graph_execution','execution_id':p['execution']['execution_id']})))
        assert value['status']=='recorded' and value['execution']==p['execution'],value
finally:agentc.shutdown()
""", str(path)], env=env, capture_output=True, text=True, timeout=60)
    assert restart.returncode == 0, restart.stderr
    sql = r"""
import json,sqlite3,hashlib,sys
from pathlib import Path
assert not any(n=='agentc' or n.startswith('agentc.') for n in sys.modules)
p=Path(sys.argv[1]);proof=json.loads(p.read_text());c=sqlite3.connect(p.parent/'cost_model.db')
assert proof['sdk_sends']==c.execute('SELECT COUNT(*) FROM provider_receipt_invocation').fetchone()[0]
assert c.execute('SELECT COUNT(*) FROM provider_graph_validation_verdict').fetchone()[0]==1
assert c.execute('SELECT COUNT(*) FROM provider_graph_role_execution').fetchone()[0]==int(proof['execution'] is not None)
invocation,subject,hash_,raw=c.execute('SELECT * FROM provider_graph_validation_verdict').fetchone()
v=json.loads(raw);assert hashlib.sha256(raw.encode()).hexdigest()==hash_
assert v['invocation_id']==invocation and v['subject_invocation_id']==subject and v['contract_sha256']==proof['contract_sha256']
assert v['decision']==('accepted' if proof['case']=='accepted' else 'rejected' if proof['case']=='rejected' else 'unknown')
for id,field in [(subject,'subject_receipt_sha256'),(invocation,'validator_receipt_sha256')]:
    call,done=c.execute('SELECT invocation_json,completion_json FROM provider_receipt_invocation WHERE invocation_id=?',(id,)).fetchone()
    assert hashlib.sha256(('{"completion":'+done+',"invocation":'+call+'}').encode()).hexdigest()==v[field]
if proof['execution']:
    assert hashlib.sha256(('['+raw+']').encode()).hexdigest()==proof['execution']['validation_sha256']
    assert proof['execution']['selected_results'][0]['slot']['ordinal']==(2 if proof['case']=='rejected' else 0)
else:
    assert proof['acquisition']['persistence_failed_batches']==1
    assert c.execute('SELECT COUNT(*) FROM provider_receipt_invocation WHERE logical_call=1').fetchone()[0]==0
for table in ['provider_graph_quality_event','provider_graph_role_pair']:
    if c.execute("SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name=?)",(table,)).fetchone()[0]:assert c.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]==0
assert not c.execute('PRAGMA foreign_key_check').fetchall()
"""
    audit = subprocess.run([sys.executable, "-I", "-c", sql, str(path)], capture_output=True, text=True, timeout=30)
    assert audit.returncode == 0, audit.stderr


DYNAMIC_PRIMARY_SDK = r"""
import agentc,openai,httpx,json,time,os,hashlib
from agentc._optimizer import _plan_call as plan_call
from agentc._patches._optimizer_glue import build_call_dict_openai
from agentc._provider_receipts import ProviderReceiptBatch,bind_plan,note_logical_call
from agentc._prepared_plan import prepared_plan
from agentc._evaluation_policy import FrozenEvaluationPolicy,evaluation_policy_scope
from agentc._batch_observation import comparison_fingerprint,GRAPH_COMPARISON_VERSION
quantity=os.environ['AGENTC_DYNAMIC_DEPLOY_QUANTITY'];sends=[];intermediates=set()
strong='gpt-4o-2024-11-20';cheap='gpt-4o-mini-2024-07-18';protocol='openai.chat.completions.v1'
def handler(request):
    body=json.loads(request.content);content=body['messages'][0]['content'];sends.append(body['model'])
    if content.startswith('[0]'):
        output='PRIVATE_INTERMEDIATE_'+content[3:];intermediates.add(output)
    else:
        assert content.startswith('[1]') and content[3:] in intermediates
        output='PRIVATE_FINAL_'+content[3:]
    usage={'prompt_tokens':2,'completion_tokens':1,'total_tokens':3,'prompt_tokens_details':{'cached_tokens':0}}
    if quantity=='billed_usd':usage.update(cost=.01 if body['model']==strong else .001,is_byok=False)
    return httpx.Response(200,json={'id':'fixture','object':'chat.completion','created':0,'model':body['model'],
        'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':output}}],'service_tier':'default','usage':usage})
client=openai.OpenAI(api_key='fixture',max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(handler)))
policy=FrozenEvaluationPolicy.from_configuration(mode='best_static_joint',calibration_lock_sha256='a'*64,
    configuration={'strong_model':strong,'cheap_model':cheap,'divergence_threshold':.05,'static_plan':{'target_model':cheap,'rewrites':[]},'router':None,'rewrite_by_target':{}})
agentc.init();scope=agentc.tariff_accounting() if quantity!='billed_usd' else None
if scope:scope.__enter__()
reference=False;manual=None;change_second=False;application=None
try:
    def task(item):
        global application
        value=item
        for step in range(2):
            callsite='dynamic-primary-node-'+str(step)
            body={'model':strong,'messages':[{'role':'user','content':'['+str(step)+']'+value}],
                'max_tokens':32 if change_second and step==1 else 16}
            with evaluation_policy_scope(policy):
                source=build_call_dict_openai(body,call_site_id=callsite,trace_id_hex='0'*32,span_id_hex=f'{step+1:016x}',client=client)
                plan=plan_call(source,observation_only=reference)
            application=json.loads(plan.source_call_json)['parameters']['extra']['agentc_route_context']['application_version']['sha256']
            if manual is not None:note_logical_call(step);bind_plan(plan)
            with prepared_plan(plan,protocol,body,callsite):
                value=client.chat.completions.create(**body).choices[0].message.content
        return value
    def cost(driver):
        value=json.loads(agentc._native.optimize_record_provider_receipt(json.dumps({'operation':'graph_acquisition_costs','driver_id':driver})))
        assert value['status']=='recorded',value
        return value['acquisition']
    with agentc.application_version('dynamic-primary-sdk-v1'),agentc.optimization_scope('dynamic-primary-deployment',optimize=True):
        for _ in range(20):
            observed=[];reference=True
            rvalue=agentc.parallel_map(task,['TRAIN'],max_workers=1,on_observation=observed.append,comparison='graph');r=observed[-1]
            reference=False
            cvalue=agentc.parallel_map(task,['TRAIN'],max_workers=1,on_observation=observed.append,comparison='graph');c=observed[-1]
            assert rvalue==cvalue and r.graph_execution_id and c.graph_execution_id,(r.status,r.reason,c.status,c.reason)
            source=json.loads(agentc._native.optimize_compare_provider_graph(r.graph_execution_id,c.graph_execution_id))
            assert source['status']=='recorded',source
        assert source['quality']['lifetime_pairs']==20 and len(sends)==80
        callback=hashlib.sha256(('map-callback-v1\0'+task.__module__+'.'+task.__qualname__).encode()).hexdigest()
        driver=hashlib.sha256(json.dumps({'version':'provider-graph-acquisition-v1','application_version':application,'callback_version':callback,'item_count':1},sort_keys=True,separators=(',',':')).encode()).hexdigest()
        before=cost(driver);money={'quantity':quantity}
        if quantity!='billed_usd':money.update(snapshot_sha256=before['costs']['published_token_tariff']['snapshot_sha256'],max_expected_nanodollars=None)
        request={'kind':'primary','source_pair_ids':[source['pair_id']],'window_size':source['quality']['window_size'],'remaining_batches':1000,'policy':{
            'selection':{'objective':'cost','min_paired_observations':20,'now_us':0,'max_profile_age_us':24*60*60*1000000,'divergence_exposure_budget':source['quality']['policy']['exposure_budget'],'max_expected_cost_usd':None,'max_expected_latency_ms':None},
            'quality':source['quality']['policy'],'money':money,'max_workers':1,'max_physical_invocations':2}}
        manual=ProviderReceiptBatch(application,callback,1);input_hash=comparison_fingerprint(['DEPLOY'],version=GRAPH_COMPARISON_VERSION)
        start=time.perf_counter();decision=manual.prepare_guarded_primary_graph(c.graph_execution_id,None,input_hash,request,max_workers=1)
        assert manual.prepare_guarded_primary_graph(c.graph_execution_id,None,input_hash,request,max_workers=1)==decision
        with manual.item(0):output=task('DEPLOY')
        manual.seal([2]);manual.finish_economics((time.perf_counter()-start)*1000)
        result={'version':GRAPH_COMPARISON_VERSION,'input_sha256':input_hash,'output_sha256':comparison_fingerprint([output],version=GRAPH_COMPARISON_VERSION)}
        e=manual.finish_guarded_primary_graph(result);assert manual.finish_guarded_primary_graph(result)==e
        assert e['key']==decision['selected_key'] and e['totals']['call_count']==2 and len(sends)==82
        assert e['spec']['dependencies']==[{'producer':{'item':0,'call':0},'consumer':{'item':0,'call':1}}]
        after=cost(driver);assert after['costs']==before['costs'] and after['economics']==before['economics']
        assert after['deployment']['batches']==[manual.batch_id] and after['deployment']['costs']['physical_invocations']==2
        deployed=manual.batch_id;manual=ProviderReceiptBatch(application,callback,1)
        input2=comparison_fingerprint(['HELD'],version=GRAPH_COMPARISON_VERSION)
        start=time.perf_counter()
        second=manual.prepare_guarded_primary_graph(c.graph_execution_id,None,input2,request,max_workers=1)
        assert second['request']['remaining_batches']==999
        change_second=True
        try:
            with manual.item(0):task('HELD')
        except RuntimeError:pass
        else:raise AssertionError('changed dynamic node controls reached HTTP')
        assert len(sends)==83 and manual.failed
        manual.seal([2]);manual.finish_economics((time.perf_counter()-start)*1000)
        try:manual.finish_guarded_primary_graph({'version':GRAPH_COMPARISON_VERSION,'input_sha256':input2,'output_sha256':'f'*64})
        except RuntimeError:pass
        else:raise AssertionError('partial dynamic workflow was published')
        final=cost(driver);assert final['economics']['persistence_failed_batches']==1
        assert final['costs']['billed_usd'] is None and final['costs']['published_token_tariff'] is None
        proof={'quantity':quantity,'sdk_sends':len(sends),'source':source,'before':before,'after':after,'final':final,
            'execution':e,'deployment_batch':deployed,'held_batch':manual.batch_id}
        assert 'PRIVATE_' not in json.dumps(proof)
        print(json.dumps(proof))
finally:
    if scope:scope.__exit__(None,None,None)
    agentc.shutdown();client.close()
"""


@pytest.mark.parametrize("quantity", ["billed_usd", "published_token_tariff_usd"])
def test_dynamic_primary_sdk_uses_previous_response_and_retains_partial_work(tmp_path, quantity):
    env = {
        **os.environ, "AGENTC_STORAGE_PATH": str(tmp_path), "AGENTC_OPTIMIZE": "1",
        "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": "1000", "AGENTC_OPTIMIZE_EXPLORATION": "0",
        "AGENTC_OPTIMIZE_SHADOW": "0", "AGENTC_CAPTURE_CONTENT": "0",
        "AGENTC_CAPTURE_EMBEDDINGS": "0", "AGENTC_DYNAMIC_DEPLOY_QUANTITY": quantity,
        "AGENTC_EVAL_PLANNER_MODE": "best_static_joint",
    }
    result = subprocess.run([sys.executable, "-I", "-c", DYNAMIC_PRIMARY_SDK], env=env, capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stderr
    path = tmp_path / "provider-graph-dynamic-deployment-sdk-proof.json"
    path.write_text(result.stdout)
    restart = subprocess.run([sys.executable, "-I", "-c", r"""
import agentc,json,sys
from pathlib import Path
p=json.loads(Path(sys.argv[1]).read_text());agentc.init()
try:
    value=json.loads(agentc._native.optimize_record_provider_receipt(json.dumps({'operation':'graph_acquisition_costs','driver_id':p['final']['economics']['driver_id']})))
    assert value['status']=='recorded' and value['acquisition']==p['final'],value
    e=p['execution'];loaded=json.loads(agentc._native.optimize_record_provider_receipt(json.dumps({'operation':'graph_execution_costs','kind':'primary','execution_id':e['execution_id']})))
    assert loaded['status']=='recorded' and loaded['projection']['journal_sha256']==e['journal_sha256'],loaded
finally:agentc.shutdown()
""", str(path)], env=env, capture_output=True, text=True, timeout=90)
    assert restart.returncode == 0, restart.stderr
    sql = r"""
import json,sqlite3,hashlib,sys
from pathlib import Path
assert not any(n=='agentc' or n.startswith('agentc.') for n in sys.modules)
p=Path(sys.argv[1]);proof=json.loads(p.read_text());c=sqlite3.connect(p.parent/'cost_model.db')
assert proof['sdk_sends']==c.execute('SELECT COUNT(*) FROM provider_receipt_invocation').fetchone()[0]==83
assert c.execute('SELECT COUNT(*) FROM provider_graph_execution').fetchone()[0]==41
assert c.execute('SELECT COUNT(*) FROM provider_graph_pair').fetchone()[0]==20
assert c.execute('SELECT COUNT(*) FROM provider_graph_quality_event').fetchone()[0]==20
assert c.execute('SELECT COUNT(*) FROM provider_graph_deployment').fetchone()[0]==2
assert c.execute('SELECT COUNT(*) FROM provider_graph_deployment_node').fetchone()[0]==3
for lease,batch,hash_,raw in c.execute('SELECT * FROM provider_graph_deployment'):
    assert hashlib.sha256(raw.encode()).hexdigest()==hash_
    a=json.loads(raw);l=json.loads(c.execute('SELECT lease_json FROM provider_graph_preparation WHERE lease_id=?',(lease,)).fetchone()[0])
    assert l['deployment_id']==hash_ and a['request']['dynamic_primary_nodes'] and not a['request'].get('primary_bindings')
    assert a['decision']['request']['remaining_batches']==1001-a['ordinal']
    bindings=c.execute('SELECT item,logical_call,binding_sha256,binding_json FROM provider_graph_deployment_node WHERE lease_id=? ORDER BY item,logical_call',(lease,)).fetchall()
    assert len(bindings)==(2 if batch==proof['deployment_batch'] else 1)
    for item,call,binding_hash,binding_raw in bindings:
        b=json.loads(binding_raw);assert hashlib.sha256(binding_raw.encode()).hexdigest()==binding_hash
        node_hash,node_raw=c.execute('SELECT node_sha256,node_json FROM provider_graph_prepared_node WHERE lease_id=? AND item=? AND logical_call=?',(lease,item,call)).fetchone()
        assert hashlib.sha256(node_raw.encode()).hexdigest()==node_hash and json.loads(node_raw)==b['node']
        invocation=json.loads(c.execute('SELECT invocation_json FROM provider_receipt_invocation WHERE batch_id=? AND item=? AND logical_call=?',(batch,item,call)).fetchone()[0])
        assert invocation['request_sha256']==b['request_sha256'] and invocation['transport_sha256']==b['transport_sha256']
        assert invocation['logical_request_sha256']==b['node']['logical_request_sha256']
assert c.execute('SELECT COUNT(*) FROM provider_receipt_invocation WHERE batch_id=? AND logical_call=1',(proof['held_batch'],)).fetchone()[0]==0
assert proof['after']['costs']==proof['before']['costs'] and proof['final']['deployment']['costs']['physical_invocations']==2
assert not c.execute('PRAGMA foreign_key_check').fetchall()
"""
    audit = subprocess.run([sys.executable, "-I", "-c", sql, str(path)], capture_output=True, text=True, timeout=30)
    assert audit.returncode == 0, audit.stderr


GUARDED_PRIMARY_SDK = r"""
import agentc,openai,httpx,json,time,os
from agentc._optimizer import plan_call
from agentc._patches._optimizer_glue import build_call_dict_openai
from agentc._provider_receipts import ProviderReceiptBatch,bind_plan,note_logical_call,_request_fingerprint,_transport_fingerprint
from agentc._prepared_plan import prepared_plan
from agentc._evaluation_policy import FrozenEvaluationPolicy,evaluation_policy_scope
from agentc._batch_observation import comparison_fingerprint,GRAPH_COMPARISON_VERSION
quantity=os.environ['AGENTC_PRIMARY_DEPLOY_QUANTITY'];sends=[]
strong='gpt-4o-2024-11-20';cheap='gpt-4o-mini-2024-07-18';protocol='openai.chat.completions.v1'
def handler(request):
    body=json.loads(request.content);sends.append(body['model'])
    usage={'prompt_tokens':2,'completion_tokens':1,'total_tokens':3,'prompt_tokens_details':{'cached_tokens':0}}
    if quantity=='billed_usd':usage.update(cost=.01 if body['model']==strong else .001,is_byok=False)
    return httpx.Response(200,json={'id':'fixture','object':'chat.completion','created':0,'model':body['model'],
        'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':'PRIVATE_PRIMARY_RESULT'}}],'service_tier':'default','usage':usage})
client=openai.OpenAI(api_key='fixture',max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(handler)))
policy=FrozenEvaluationPolicy.from_configuration(mode='best_static_joint',calibration_lock_sha256='a'*64,
    configuration={'strong_model':strong,'cheap_model':cheap,'divergence_threshold':.05,'static_plan':{'target_model':cheap,'rewrites':[]},'router':None,'rewrite_by_target':{}})
agentc.init();scope=agentc.tariff_accounting() if quantity!='billed_usd' else None
if scope:scope.__enter__()
try:
    def prepare(reference=False,value='TRAIN'):
        body={'model':strong,'messages':[{'role':'user','content':'PRIVATE_PRIMARY_'+value}],'max_tokens':16}
        with evaluation_policy_scope(policy):
            source=build_call_dict_openai(body,call_site_id='primary-deploy-node',trace_id_hex='0'*32,span_id_hex='1'*16,client=client)
            plan=plan_call(source,observation_only=reference)
        described=json.loads(agentc._native.optimize_describe_provider_graph_node(json.dumps({'item':0,'call':0}),plan.source_call_json,plan.raw_json))
        assert described['status']=='recorded',described
        node=described['node'];actual={**body,'model':strong if reference else cheap}
        assert node['plan']['target_model_id']==actual['model']
        binding={'node':node,'request_sha256':_request_fingerprint(actual,protocol),'transport_sha256':_transport_fingerprint(client.chat.completions,protocol)}
        return plan,body,binding
    def task(item):
        with prepared_plan(active,protocol,body,'primary-deploy-node'):
            return client.chat.completions.create(**body).choices[0].message.content
    def cost(driver):
        value=json.loads(agentc._native.optimize_record_provider_receipt(json.dumps({'operation':'graph_acquisition_costs','driver_id':driver})))
        assert value['status']=='recorded',value
        return value['acquisition']
    with agentc.application_version('guarded-primary-sdk-v1'),agentc.optimization_scope('primary-deployment',optimize=True):
        source=None
        for _ in range(20):
            observed=[];active,body,_=prepare(True)
            assert agentc.parallel_map(task,['TRAIN'],max_workers=1,on_observation=observed.append,comparison='graph')==['PRIVATE_PRIMARY_RESULT']
            r=observed[-1];active,body,_=prepare()
            assert agentc.parallel_map(task,['TRAIN'],max_workers=1,on_observation=observed.append,comparison='graph')==['PRIVATE_PRIMARY_RESULT']
            c=observed[-1]
            assert r.graph_execution_id and c.graph_execution_id
            source=json.loads(agentc._native.optimize_compare_provider_graph(r.graph_execution_id,c.graph_execution_id))
            assert source['status']=='recorded',source
        assert source['quality']['lifetime_pairs']==20 and len(sends)==40
        original=json.loads(agentc._native.optimize_record_provider_receipt(json.dumps({'operation':'graph_execution_costs','kind':'primary','execution_id':c.graph_execution_id})))['projection']
        application=json.loads(active.source_call_json)['parameters']['extra']['agentc_route_context']['application_version']['sha256']
        from hashlib import sha256
        callback=sha256(('map-callback-v1\0'+task.__module__+'.'+task.__qualname__).encode()).hexdigest()
        driver=sha256(json.dumps({'version':'provider-graph-acquisition-v1','application_version':application,'callback_version':callback,'item_count':1},sort_keys=True,separators=(',',':')).encode()).hexdigest()
        before=cost(driver)
        money={'quantity':quantity}
        if quantity!='billed_usd':money.update(snapshot_sha256=before['costs']['published_token_tariff']['snapshot_sha256'],max_expected_nanodollars=None)
        request={'kind':'primary','source_pair_ids':[source['pair_id']],'window_size':source['quality']['window_size'],'remaining_batches':1000,'policy':{
            'selection':{'objective':'cost','min_paired_observations':20,'now_us':0,'max_profile_age_us':24*60*60*1000000,'divergence_exposure_budget':source['quality']['policy']['exposure_budget'],'max_expected_cost_usd':None,'max_expected_latency_ms':None},
            'quality':source['quality']['policy'],'money':money,'max_workers':1,'max_physical_invocations':1}}
        active,body,binding=prepare(value='DEPLOY_ONE')
        input_hash=comparison_fingerprint(['DEPLOY_ONE'],version=GRAPH_COMPARISON_VERSION)
        batch=ProviderReceiptBatch(application,callback,1)
        start=time.perf_counter();decision=batch.prepare_guarded_primary_graph(c.graph_execution_id,[binding],input_hash,request,max_workers=1)
        assert batch.prepare_guarded_primary_graph(c.graph_execution_id,[binding],input_hash,request,max_workers=1)==decision
        with batch.item(0):
            note_logical_call(0);bind_plan(active);output=task('DEPLOY_ONE')
        batch.seal([1]);batch.finish_economics((time.perf_counter()-start)*1000)
        result={'version':GRAPH_COMPARISON_VERSION,'input_sha256':input_hash,'output_sha256':comparison_fingerprint([output],version=GRAPH_COMPARISON_VERSION)}
        e=batch.finish_guarded_primary_graph(result)
        assert batch.finish_guarded_primary_graph(result)==e
        assert e['key']==decision['selected_key'] and e['totals']['call_count']==1 and len(sends)==41
        after=cost(driver);assert after['costs']==before['costs'] and after['economics']==before['economics']
        assert after['deployment']['batches']==[batch.batch_id] and after['deployment']['costs']['physical_invocations']==1
        active,body,binding=prepare(value='DEPLOY_TWO');pending=ProviderReceiptBatch(application,callback,1)
        input2=comparison_fingerprint(['DEPLOY_TWO'],version=GRAPH_COMPARISON_VERSION)
        second=pending.prepare_guarded_primary_graph(c.graph_execution_id,[binding],input2,request,max_workers=1)
        assert second['request']['remaining_batches']==999
        active,body,_=prepare(value='CHANGED_AFTER_APPROVAL')
        try:
            with pending.item(0):note_logical_call(0);bind_plan(active);task('CHANGED_AFTER_APPROVAL')
        except RuntimeError:pass
        else:raise AssertionError('changed upcoming primary request reached HTTP')
        assert len(sends)==41 and pending.failed
        pending.seal([1]);pending.finish_economics(0.)
        final=cost(driver);assert final['economics']['persistence_failed_batches']==1
        assert final['costs']['billed_usd'] is None and final['costs']['published_token_tariff'] is None
        proof={'quantity':quantity,'sdk_sends':len(sends),'source':source,'original':original,'before':before,'after':after,'final':final,
            'execution':e,'deployment_batch':batch.batch_id,'held_batch':pending.batch_id}
        assert 'PRIVATE_' not in json.dumps(proof)
        print(json.dumps(proof))
finally:
    if scope:scope.__exit__(None,None,None)
    agentc.shutdown();client.close()
"""


@pytest.mark.parametrize("quantity", ["billed_usd", "published_token_tariff_usd"])
def test_guarded_primary_sdk_deployment_binds_requests_and_publishes_original_receipts(tmp_path, quantity):
    env = {
        **os.environ, "AGENTC_STORAGE_PATH": str(tmp_path), "AGENTC_OPTIMIZE": "1",
        "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": "1000", "AGENTC_OPTIMIZE_EXPLORATION": "0",
        "AGENTC_OPTIMIZE_SHADOW": "0", "AGENTC_CAPTURE_CONTENT": "0",
        "AGENTC_CAPTURE_EMBEDDINGS": "0", "AGENTC_PRIMARY_DEPLOY_QUANTITY": quantity,
        "AGENTC_EVAL_PLANNER_MODE": "best_static_joint",
    }
    result = subprocess.run([sys.executable, "-I", "-c", GUARDED_PRIMARY_SDK], env=env, capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stderr
    path = tmp_path / "provider-graph-primary-deployment-sdk-proof.json"
    path.write_text(result.stdout)
    restart = subprocess.run([sys.executable, "-I", "-c", r"""
import agentc,json,sys
from pathlib import Path
p=json.loads(Path(sys.argv[1]).read_text());agentc.init()
try:
    value=json.loads(agentc._native.optimize_record_provider_receipt(json.dumps({'operation':'graph_acquisition_costs','driver_id':p['final']['economics']['driver_id']})))
    assert value['status']=='recorded' and value['acquisition']==p['final'],value
    e=p['execution'];loaded=json.loads(agentc._native.optimize_record_provider_receipt(json.dumps({'operation':'graph_execution_costs','kind':'primary','execution_id':e['execution_id']})))
    assert loaded['status']=='recorded' and loaded['projection']['journal_sha256']==e['journal_sha256'],loaded
finally:agentc.shutdown()
""", str(path)], env=env, capture_output=True, text=True, timeout=90)
    assert restart.returncode == 0, restart.stderr
    sql = r"""
import json,sqlite3,hashlib,sys
from pathlib import Path
assert not any(n=='agentc' or n.startswith('agentc.') for n in sys.modules)
p=Path(sys.argv[1]);proof=json.loads(p.read_text());c=sqlite3.connect(p.parent/'cost_model.db')
assert proof['sdk_sends']==c.execute('SELECT COUNT(*) FROM provider_receipt_invocation').fetchone()[0]==41
assert c.execute('SELECT COUNT(*) FROM provider_graph_execution').fetchone()[0]==41
assert c.execute('SELECT COUNT(*) FROM provider_graph_pair').fetchone()[0]==20
assert c.execute('SELECT COUNT(*) FROM provider_graph_quality_event').fetchone()[0]==20
assert c.execute('SELECT COUNT(*) FROM provider_graph_deployment').fetchone()[0]==2
assert c.execute('SELECT COUNT(*) FROM provider_receipt_invocation WHERE batch_id=?',(proof['held_batch'],)).fetchone()[0]==0
for id,batch,hash_,raw in c.execute('SELECT * FROM provider_graph_deployment'):
    assert hashlib.sha256(raw.encode()).hexdigest()==hash_
    a=json.loads(raw);l=json.loads(c.execute('SELECT lease_json FROM provider_graph_preparation WHERE lease_id=?',(id,)).fetchone()[0])
    assert l['deployment_id']==hash_ and l['template'] and l.get('role_program_id') is None
    assert a['request']['admission']['kind']=='primary' and len(a['request']['primary_bindings'])==1
    assert a['decision']['request']['remaining_batches']==1001-a['ordinal']
    start=raw.index('"decision":')+len('"decision":');d,end=json.JSONDecoder().raw_decode(raw,start)
    digest=d['evidence_sha256'];part=raw[start:end];needle='"evidence_sha256":"'+digest+'"'
    assert part.count(needle)==1 and hashlib.sha256(part.replace(needle,'"evidence_sha256":""').encode()).hexdigest()==digest
assert proof['after']['costs']==proof['before']['costs'] and proof['final']['deployment']['costs']['physical_invocations']==1
assert not c.execute('PRAGMA foreign_key_check').fetchall()
"""
    audit = subprocess.run([sys.executable, "-I", "-c", sql, str(path)], capture_output=True, text=True, timeout=30)
    assert audit.returncode == 0, audit.stderr


GUARDED_DEPLOYMENT_SDK = r"""
import agentc,openai,httpx,json,time,os
from agentc._optimizer import plan_call
from agentc._patches._optimizer_glue import build_call_dict_openai
from agentc._provider_receipts import ProviderReceiptBatch,instrument_provider,bind_plan,note_logical_call,_request_fingerprint,_transport_fingerprint
from agentc._dispatch_context import provider_dispatch
from agentc._evaluation_policy import FrozenEvaluationPolicy,evaluation_policy_scope
from agentc._batch_observation import comparison_fingerprint,GRAPH_COMPARISON_VERSION
quantity=os.environ['AGENTC_GRAPH_DEPLOY_QUANTITY'];sends=[];metadata=[];generations={}
strong='openai/gpt-4o' if quantity=='billed_usd' else 'gpt-4o-2024-11-20'
cheap='openai/gpt-4o-mini' if quantity=='billed_usd' else 'gpt-4o-mini-2024-07-18';protocol='openai.chat.completions.v1'
def handler(request):
    if request.method=='GET':
        generation=request.url.params['id'];metadata.append(generation);model=generations[generation]
        return httpx.Response(200,json={'data':{'id':generation,'request_id':'req-'+generation,'model':model,'native_tokens_prompt':2,'native_tokens_completion':1,'total_cost':.01 if model==strong else .001,'is_byok':False,'cancelled':False,'finish_reason':'stop'}})
    body=json.loads(request.content);sends.append(body['model'])
    generation='gen-deploy-'+str(len(sends));generations[generation]=body['model']
    usage={'prompt_tokens':2,'completion_tokens':1,'total_tokens':3,'prompt_tokens_details':{'cached_tokens':0}}
    if quantity=='billed_usd':usage.update(cost=.01 if body['model']==strong else .001,is_byok=False)
    return httpx.Response(200,headers={'x-request-id':'req-'+generation},json={'id':generation,'object':'chat.completion','created':0,'model':body['model'],
        'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':'PRIVATE_DEPLOY_RESULT'}}],
        'service_tier':'default','usage':usage})
client=openai.OpenAI(api_key='fixture',base_url='https://openrouter.ai/api/v1' if quantity=='billed_usd' else 'https://api.openai.com/v1',max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(handler)))
policy=FrozenEvaluationPolicy.from_configuration(mode='best_static_joint',calibration_lock_sha256='a'*64,
    configuration={'strong_model':strong,'cheap_model':cheap,'divergence_threshold':.05,
        'static_plan':{'target_model':cheap,'rewrites':[]},'router':None,'rewrite_by_target':{}})
agentc.init();scope=agentc.tariff_accounting() if quantity!='billed_usd' else None
if scope:scope.__enter__()
try:
    def program(reference=False,value='TRAIN'):
        original={'model':strong,'messages':[{'role':'user','content':'PRIVATE_DEPLOY_'+value}],'max_tokens':16}
        with agentc.application_version('guarded-role-sdk-v1'),evaluation_policy_scope(policy):
            source=build_call_dict_openai(original,call_site_id='guarded-role',trace_id_hex='0'*32,span_id_hex='1'*16,client=client)
            plan=plan_call(source,observation_only=reference)
        node=json.loads(agentc._native.optimize_describe_provider_graph_node(json.dumps({'item':0,'call':0}),plan.source_call_json,plan.raw_json))
        assert node['status']=='recorded',node
        node=node['node'];body={**original,'model':strong if reference else cheap}
        assert node['plan']['requested_model_id']==strong and node['plan']['target_model_id']==body['model']
        application=json.loads(plan.source_call_json)['parameters']['extra']['agentc_route_context']['application_version']['sha256']
        graph={'version':'provider-call-graph-v1','calls':{'application_version':application,'callback_version':'guarded-role-sdk-v1','item_count':1,'max_workers':1,
            'branches':[{'position':node['position'],'call_site_version':node['key']['call_site_version'],'plan':node['plan']}]},'dependencies':[],'results':{'kind':'callback_items','items':[0]}}
        roles=['primary'] if reference else ['primary','validation']
        p={'program':{'version':'provider-role-graph-v1','graph':graph,'dispatches':[{'slot':{'position':node['position'],'ordinal':n},'role':role,'call_site_version':node['key']['call_site_version'],'plan':node['plan'],'activation':{'kind':'always'}} for n,role in enumerate(roles)]},
            'bindings':[{'slot':{'position':node['position'],'ordinal':n},'runtime':node['runtime'],'request_sha256':_request_fingerprint(body,protocol),'transport_sha256':_transport_fingerprint(client.chat.completions,protocol)} for n in range(len(roles))],
            'logical_requests':[{'position':node['position'],'sha256':node['logical_request_sha256']}],
            'source_contracts':[{'position':node['position'],'sha256':node['source_contract_sha256']}]}
        return p,plan,body,comparison_fingerprint([value],version=GRAPH_COMPARISON_VERSION)
    def batch(p):return ProviderReceiptBatch(p['program']['graph']['calls']['application_version'],'guarded-role-sdk-v1',1)
    def invoke(b,plan,body,role='primary'):
        with b.item(0):
            note_logical_call(0);bind_plan(plan)
            provider_call=instrument_provider(client.chat.completions.create.__wrapped__,client.chat.completions,protocol)
            with provider_dispatch(role,plan=plan,primary_plan=plan,dispatch_plan=plan):return provider_call(**body)
    def execute(p,plan,body,input_hash,*,admission=None):
        b=batch(p);start=time.perf_counter()
        if admission is None:b.prepare_role_graph(p,input_hash)
        else:
            decision=b.prepare_guarded_role_graph(p,input_hash,admission)
            retry=b.prepare_guarded_role_graph(p,input_hash,admission);assert retry==decision
        result=None
        for d in p['program']['dispatches']:result=invoke(b,plan,body,d['role'])
        b.seal([1]);b.finish_economics((time.perf_counter()-start)*1000)
        comparison={'version':GRAPH_COMPARISON_VERSION,'input_sha256':input_hash,'output_sha256':comparison_fingerprint([result.choices[0].message.content],version=GRAPH_COMPARISON_VERSION)}
        done=json.loads(agentc._native.optimize_record_provider_receipt(json.dumps({'operation':'finish_role_graph','lease_id':b.graph_preparation_id,'comparison':comparison})))
        assert done['status']=='recorded',done
        return b,done['execution']
    rp,rplan,rbody,input_hash=program(True);cp,cplan,cbody,cinput=program()
    assert input_hash==cinput and rp['source_contracts']==cp['source_contracts']
    pair=None
    for _ in range(20):
        _,r=execute(rp,rplan,rbody,input_hash);_,c=execute(cp,cplan,cbody,input_hash)
        pair=json.loads(agentc._native.optimize_compare_provider_role_graph(r['execution_id'],c['execution_id']))
        assert pair['status']=='recorded',pair
    assert len(sends)==60 and pair['quality']['lifetime_pairs']==20
    def costs():
        response=json.loads(agentc._native.optimize_record_provider_receipt(json.dumps({'operation':'graph_acquisition_costs','driver_id':c['acquisition']['driver_id']})))
        assert response['status']=='recorded',response
        return response['acquisition']
    before=costs()
    money={'quantity':quantity}
    if quantity!='billed_usd':money.update(snapshot_sha256=before['costs']['published_token_tariff']['snapshot_sha256'],max_expected_nanodollars=None)
    selection={'objective':'cost','min_paired_observations':20,'now_us':0,'max_profile_age_us':24*60*60*1000000,
        'divergence_exposure_budget':pair['quality']['policy']['exposure_budget'],'max_expected_cost_usd':None,'max_expected_latency_ms':None}
    request={'kind':'roles','source_pair_ids':[pair['pair_id']],'window_size':pair['quality']['window_size'],'remaining_batches':1000,
        'policy':{'selection':selection,'quality':pair['quality']['policy'],'money':money,'max_workers':1,'max_physical_invocations':2}}
    p,plan,body,new_input=program(value='DEPLOY_ONE')
    deployed,e=execute(p,plan,body,new_input,admission=request)
    if quantity=='billed_usd':
        receipts=agentc.provider_billing_receipts(deployed.batch_id)
        assert len(receipts)==2
        from agentc._provider_receipts import _provider_identity_fingerprint
        receipt=receipts[0]
        generation=next(g for g in generations if _provider_identity_fingerprint(g)==receipt.generation_sha256)
        reconciled=agentc.reconcile_openrouter_generation(receipt,generation_id=generation,client=client)
        assert reconciled.status=='recorded',reconciled
        assert len(metadata)==1
    after=costs()
    assert after['costs']==before['costs'] and after['economics']==before['economics']
    assert after['deployment']['batches']==[deployed.batch_id] and after['deployment']['costs']['physical_invocations']==2
    resource=json.loads(agentc._native.optimize_record_provider_receipt(json.dumps({'operation':'graph_execution_costs','kind':'roles','execution_id':e['execution_id']})))['projection']
    verification=after['deployment']['economics']['billing_verification_latency_ms']
    assert resource['resource_latency_ms']['lower_ms']==e['acquisition']['foreground_latency_ms']
    assert resource['resource_latency_ms']['upper_ms']>=e['acquisition']['foreground_latency_ms']+verification
    assert verification>0 if quantity=='billed_usd' else verification==0
    assert len(sends)==62
    p2,plan2,body2,input2=program(value='DEPLOY_TWO');pending=batch(p2)
    second=pending.prepare_guarded_role_graph(p2,input2,request)
    assert second['request']['remaining_batches']==999
    extra=batch(p2);extra.prepare_role_graph(p2,input2)
    try:invoke(pending,plan2,body2)
    except RuntimeError:pass
    else:raise AssertionError('new unfinished acquisition did not hold the deployed SDK dispatch')
    assert len(sends)==62
    pending.seal([1]);pending.finish_economics(0.)
    final=costs()
    assert final['economics']['unfinished_batches']==1 and final['economics']['persistence_failed_batches']==1
    assert final['costs']['billed_usd'] is None and final['costs']['published_token_tariff'] is None
    assert final['costs']['known_billed_usd']==before['costs']['known_billed_usd']
    proof={'quantity':quantity,'sdk_sends':len(sends),'pair':pair,'before':before,'after':after,'final':final,
        'execution':e,'deployment_resource':resource,'metadata_gets':len(metadata),'deployment_batch':deployed.batch_id,'held_batch':pending.batch_id,'extra_batch':extra.batch_id}
    assert 'PRIVATE_' not in json.dumps(proof)
    print(json.dumps(proof))
finally:
    if scope:scope.__exit__(None,None,None)
    agentc.shutdown();client.close()
"""


@pytest.mark.parametrize("quantity", ["billed_usd", "published_token_tariff_usd"])
def test_guarded_role_sdk_deployment_preserves_phase_costs_and_rechecks_before_http(tmp_path, quantity):
    env = {
        **os.environ, "AGENTC_STORAGE_PATH": str(tmp_path), "AGENTC_OPTIMIZE": "1",
        "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": "1000", "AGENTC_OPTIMIZE_EXPLORATION": "0",
        "AGENTC_OPTIMIZE_SHADOW": "0", "AGENTC_CAPTURE_CONTENT": "0",
        "AGENTC_CAPTURE_EMBEDDINGS": "0", "AGENTC_GRAPH_DEPLOY_QUANTITY": quantity,
        "AGENTC_EVAL_PLANNER_MODE": "best_static_joint",
    }
    result = subprocess.run([sys.executable, "-I", "-c", GUARDED_DEPLOYMENT_SDK], env=env, capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stderr
    path = tmp_path / "provider-graph-deployment-sdk-proof.json"
    path.write_text(result.stdout)
    restart = subprocess.run([sys.executable, "-I", "-c", r"""
import agentc,json,sys
from pathlib import Path
proof=json.loads(Path(sys.argv[1]).read_text());agentc.init()
try:
    result=json.loads(agentc._native.optimize_record_provider_receipt(json.dumps({'operation':'graph_acquisition_costs','driver_id':proof['final']['economics']['driver_id']})))
    assert result['status']=='recorded' and result['acquisition']==proof['final'],result
    loaded=json.loads(agentc._native.optimize_record_provider_receipt(json.dumps({'operation':'role_graph_execution','execution_id':proof['execution']['execution_id']})))
    assert loaded['status']=='recorded' and loaded['execution']==proof['execution'],loaded
finally:agentc.shutdown()
""", str(path)], env=env, capture_output=True, text=True, timeout=90)
    assert restart.returncode == 0, restart.stderr
    sql = r"""
import json,sqlite3,hashlib,sys
from pathlib import Path
assert not any(n=='agentc' or n.startswith('agentc.') for n in sys.modules)
p=Path(sys.argv[1]);proof=json.loads(p.read_text());c=sqlite3.connect(p.parent/'cost_model.db')
assert proof['sdk_sends']==c.execute('SELECT COUNT(*) FROM provider_receipt_invocation').fetchone()[0]==62
assert c.execute('SELECT COUNT(*) FROM provider_graph_role_execution').fetchone()[0]==41
assert c.execute('SELECT COUNT(*) FROM provider_graph_role_pair').fetchone()[0]==20
assert c.execute('SELECT COUNT(*) FROM provider_graph_role_quality_event').fetchone()[0]==20
assert c.execute('SELECT COUNT(*) FROM provider_graph_deployment').fetchone()[0]==2
assert c.execute('SELECT COUNT(*) FROM provider_receipt_invocation WHERE batch_id=?',(proof['held_batch'],)).fetchone()[0]==0
for lease_id,batch,hash_,raw in c.execute('SELECT * FROM provider_graph_deployment'):
    assert hashlib.sha256(raw.encode()).hexdigest()==hash_
    approval=json.loads(raw);lease=json.loads(c.execute('SELECT lease_json FROM provider_graph_preparation WHERE lease_id=?',(lease_id,)).fetchone()[0])
    start=raw.index('"decision":')+len('"decision":')
    decision,end=json.JSONDecoder().raw_decode(raw,start)
    digest=decision['evidence_sha256'];encoded=raw[start:end]
    needle='"evidence_sha256":"'+digest+'"';assert encoded.count(needle)==1
    assert hashlib.sha256(encoded.replace(needle,'"evidence_sha256":""').encode()).hexdigest()==digest
    assert lease['deployment_id']==hash_ and lease['batch_id']==batch==approval['request']['preparation']['batch_id']
    assert approval['decision']['selected_reference'] is False
    assert approval['decision']['request']['policy']['selection']['now_us']==lease['started_at_us']
    assert approval['decision']['request']['remaining_batches']==1001-approval['ordinal']
for batch,hash_,raw in c.execute('SELECT * FROM provider_graph_foreground_input'):
    assert hashlib.sha256(raw.encode()).hexdigest()==hash_
    assert json.loads(raw)['wall_latency_ms']==c.execute('SELECT wall_latency_ms FROM provider_graph_acquisition WHERE batch_id=?',(batch,)).fetchone()[0]
assert proof['after']['costs']==proof['before']['costs']
assert proof['final']['deployment']['costs']['physical_invocations']==2
verifications=[]
for reserved,result in c.execute('SELECT result_input_json,result_json FROM provider_billing_attempt WHERE batch_id=?',(proof['deployment_batch'],)):
    assert reserved==result;verifications.append(json.loads(result)['latency_ms'])
assert sum(verifications)==proof['after']['deployment']['economics']['billing_verification_latency_ms']
assert len(verifications)==proof['metadata_gets']==(1 if proof['quantity']=='billed_usd' else 0)
assert proof['deployment_resource']['resource_latency_ms']['upper_ms']>=proof['execution']['acquisition']['foreground_latency_ms']+sum(verifications)
assert not c.execute('PRAGMA foreign_key_check').fetchall()
"""
    audit = subprocess.run([sys.executable, "-I", "-c", sql, str(path)], capture_output=True, text=True, timeout=30)
    assert audit.returncode == 0, audit.stderr


WORKER_LIMIT_SDK = r"""
import agentc,openai,httpx,json,time,threading
from agentc._optimizer import plan_call
from agentc._patches._optimizer_glue import build_call_dict_openai
from agentc._provider_receipts import ProviderReceiptBatch,instrument_provider,bind_plan,note_logical_call,_request_fingerprint,_transport_fingerprint
from agentc._dispatch_context import provider_dispatch
from agentc._batch_observation import comparison_fingerprint,GRAPH_COMPARISON_VERSION
entered=threading.Event();release=threading.Event();calls=[];errors=[]
model='gpt-4o-mini-2024-07-18';protocol='openai.chat.completions.v1'
def handler(request):
    calls.append(1);entered.set()
    assert release.wait(30),'fixture did not release its reserved worker'
    return httpx.Response(200,json={'id':'fixture','object':'chat.completion','created':0,'model':model,
        'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':'PRIVATE_WORKER_RESULT'}}],
        'usage':{'prompt_tokens':2,'completion_tokens':1,'total_tokens':3,'cost':.01,'is_byok':False}})
client=openai.OpenAI(api_key='fixture',max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(handler)))
agentc.init()
try:
    plans=[];nodes=[];bodies=[]
    with agentc.application_version('graph-worker-limit-v1'):
        for item in range(2):
            body={'model':model,'messages':[{'role':'user','content':'PRIVATE_WORKER_'+str(item)}],'max_tokens':8};bodies.append(body)
            source=build_call_dict_openai(body,call_site_id='worker-limit-'+str(item),trace_id_hex='0'*32,span_id_hex=format(item+1,'016x'),client=client)
            plan=plan_call(source,observation_only=True);plans.append(plan)
            described=json.loads(agentc._native.optimize_describe_provider_graph_node(json.dumps({'item':item,'call':0}),plan.source_call_json,plan.raw_json))
            assert described['status']=='recorded',described
            nodes.append(described['node'])
    application=json.loads(plans[0].source_call_json)['parameters']['extra']['agentc_route_context']['application_version']['sha256']
    graph={'version':'provider-call-graph-v1','calls':{'application_version':application,'callback_version':'graph-worker-limit-v1','item_count':2,'max_workers':1,
        'branches':[{'position':n['position'],'call_site_version':n['key']['call_site_version'],'plan':n['plan']} for n in nodes]},'dependencies':[],'results':{'kind':'callback_items','items':[0,1]}}
    program={'program':{'version':'provider-role-graph-v1','graph':graph,'dispatches':[{'slot':{'position':n['position'],'ordinal':0},'role':'primary','call_site_version':n['key']['call_site_version'],'plan':n['plan'],'activation':{'kind':'always'}} for n in nodes]},
        'bindings':[{'slot':{'position':n['position'],'ordinal':0},'runtime':n['runtime'],'request_sha256':_request_fingerprint(bodies[i],protocol),'transport_sha256':_transport_fingerprint(client.chat.completions,protocol)} for i,n in enumerate(nodes)],
        'logical_requests':[{'position':n['position'],'sha256':n['logical_request_sha256']} for n in nodes],
        'source_contracts':[{'position':n['position'],'sha256':n['source_contract_sha256']} for n in nodes]}
    batch=ProviderReceiptBatch(application,'graph-worker-limit-v1',2)
    batch.prepare_role_graph(program,comparison_fingerprint([0,1],version=GRAPH_COMPARISON_VERSION))
    def invoke(item):
        owner=plans[item]
        with batch.item(item):
            note_logical_call(0);bind_plan(owner)
            provider_call=instrument_provider(client.chat.completions.create.__wrapped__,client.chat.completions,protocol)
            with provider_dispatch('primary',plan=owner,primary_plan=owner,dispatch_plan=owner):
                return provider_call(**bodies[item])
    def first():
        try:invoke(0)
        except BaseException as e:errors.append(type(e).__name__)
    worker=threading.Thread(target=first);worker.start()
    try:
        assert entered.wait(15),'first SDK call did not enter HTTP'
        try:invoke(1)
        except RuntimeError:pass
        else:raise AssertionError('second SDK call exceeded the frozen worker cap')
        assert len(calls)==1
    finally:release.set();worker.join(15)
    assert not worker.is_alive() and not errors,errors
    batch.seal([1,1]);batch.finish_economics(100.)
    assert batch.graph_acquisition['physical_invocations']==1
    assert batch.graph_acquisition['persistence_failed_batches']==1 and not batch.graph_acquisition['cost_complete']
    print(json.dumps({'sdk_sends':1,'held_before_http':1,'batch_id':batch.batch_id,'lease_id':batch.graph_preparation_id,'acquisition':batch.graph_acquisition}))
finally:release.set();agentc.shutdown();client.close()
"""


def test_frozen_graph_worker_capacity_holds_an_independent_sdk_call_before_http(tmp_path):
    env = {
        **os.environ, "AGENTC_STORAGE_PATH": str(tmp_path), "AGENTC_OPTIMIZE": "1",
        "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": "1000", "AGENTC_OPTIMIZE_EXPLORATION": "0",
        "AGENTC_OPTIMIZE_SHADOW": "0", "AGENTC_CAPTURE_CONTENT": "0",
        "AGENTC_CAPTURE_EMBEDDINGS": "0",
    }
    result = subprocess.run([sys.executable, "-I", "-c", WORKER_LIMIT_SDK], env=env, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stderr
    proof = json.loads(result.stdout)
    assert proof["sdk_sends"] == proof["held_before_http"] == 1
    path = tmp_path / "provider-graph-worker-limit-sdk-proof.json"
    path.write_text(result.stdout)
    sql = r"""
import json,sqlite3,sys
from pathlib import Path
assert not any(n=='agentc' or n.startswith('agentc.') for n in sys.modules)
p=Path(sys.argv[1]);proof=json.loads(p.read_text());c=sqlite3.connect(p.parent/'cost_model.db')
assert c.execute('SELECT COUNT(*) FROM provider_receipt_invocation').fetchone()[0]==1
assert c.execute('SELECT COUNT(*) FROM provider_graph_role_execution').fetchone()[0]==0
lease=json.loads(c.execute('SELECT lease_json FROM provider_graph_preparation WHERE lease_id=?',(proof['lease_id'],)).fetchone()[0])
assert lease['max_workers']==1
assert c.execute('SELECT persistence_failed FROM provider_graph_acquisition WHERE batch_id=?',(proof['batch_id'],)).fetchone()[0]==1
assert not c.execute('PRAGMA foreign_key_check').fetchall()
"""
    audited = subprocess.run([sys.executable, "-I", "-c", sql, str(path)], capture_output=True, text=True, timeout=30)
    assert audited.returncode == 0, audited.stderr


SDK_WORKER = r"""
import json,time
from dataclasses import asdict
import agentc,openai,httpx
from agentc._evaluation_policy import FrozenEvaluationPolicy,evaluation_policy_scope
from agentc._batch_observation import refresh_parallel_observation
sends=[];reported=True
strong='gpt-4o-2024-11-20';cheap='gpt-4o-mini-2024-07-18'
def handler(request):
    body=json.loads(request.content);sends.append((body['model'],body.get('max_tokens')));time.sleep(.003)
    usage={'prompt_tokens':4,'completion_tokens':2,'total_tokens':6}
    if reported:usage.update(cost=.02,is_byok=False)
    return httpx.Response(200,json={'id':'chatcmpl-graph-fixture','object':'chat.completion','created':0,
        'model':body['model'],'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':'PRIVATE_GRAPH_RESULT'}}],
        'usage':usage})
client=openai.OpenAI(api_key='fixture',max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(handler)))
policy=FrozenEvaluationPolicy.from_configuration(mode='best_static_joint',calibration_lock_sha256='a'*64,
    configuration={'strong_model':strong,'cheap_model':cheap,'divergence_threshold':.05,
        'static_plan':{'target_model':cheap,'rewrites':[{'rule':'OutputBudget','max_output_tokens':64}]},
        'router':None,'rewrite_by_target':{}})
def task(item):
    first=client.chat.completions.create(model=strong,messages=[{'role':'user','content':'PRIVATE_GRAPH_PROMPT_'+item}],max_tokens=128)
    second=client.chat.completions.create(model=strong,messages=[{'role':'user','content':first.choices[0].message.content}],max_tokens=128)
    # The first call's result is returned, so a collector cannot invent the
    # last SDK call as the producer of the opaque callback's output.
    return first.choices[0].message.content
agentc.init()
try:
    observations=[]
    with agentc.application_version('model-rewrite-graph-sdk-v1'),agentc.optimization_scope('graph-fixture',optimize=True),evaluation_policy_scope(policy):
        for _ in range(2):
            results=agentc.parallel_map(task,['one','two'],max_workers=2,on_observation=observations.append,comparison=True)
            assert results==['PRIVATE_GRAPH_RESULT']*2
        assert len(sends)==8 and all(row==(cheap,64) for row in sends),sends
        for observation in observations:
            assert observation.status=='recorded' and observation.graph_execution_id and observation.comparison_token is None,observation
            assert observation.reason=='whole_graph_comparison_contract_required'
            assert observation.cost_source=='provider_reported' and abs(observation.cost_usd-.08)<1e-9
        assert observations[0].execution_plan_id==observations[1].execution_plan_id
        assert observations[0].graph_execution_id!=observations[1].graph_execution_id
        reported=False
        agentc.parallel_map(task,['one','two'],max_workers=2,on_observation=observations.append)
        assert observations[-1].status=='pending_billing' and observations[-1].graph_execution_id
        assert observations[-1].cost_usd is None and observations[-1].cost_source=='estimated'
        assert refresh_parallel_observation(observations[-1]) is observations[-1]
        assert len(sends)==12
    print(json.dumps({'sdk_sends':len(sends),'observations':[asdict(row) for row in observations]}))
finally:
    agentc.shutdown();client.close()
"""


def test_real_sdk_combined_graph_publication_preserves_all_nodes_and_actual_receipts(tmp_path):
    env = {
        **os.environ,
        "AGENTC_STORAGE_PATH": str(tmp_path),
        "AGENTC_OPTIMIZE": "1",
        "AGENTC_EVAL_PLANNER_MODE": "best_static_joint",
        "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": "1000",
        "AGENTC_OPTIMIZE_EXPLORATION": "0",
        "AGENTC_OPTIMIZE_SHADOW": "0",
        "AGENTC_CAPTURE_CONTENT": "0",
        "AGENTC_CAPTURE_EMBEDDINGS": "0",
    }
    result = subprocess.run(
        [sys.executable, "-I", "-c", SDK_WORKER], env=env,
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    proof = json.loads(result.stdout)
    assert proof["sdk_sends"] == 12
    (tmp_path / "provider-graph-sdk-proof.json").write_text(result.stdout)
    audit_script = r"""
import json,sqlite3,hashlib,sys
from pathlib import Path
assert not any(n=='agentc' or n.startswith('agentc.') for n in sys.modules)
conn=sqlite3.connect(Path(sys.argv[1])/'cost_model.db')
assert conn.execute('SELECT COUNT(*) FROM provider_receipt_invocation').fetchone()[0]==12
rows=conn.execute('SELECT execution_sha256,execution_json FROM provider_graph_execution').fetchall()
assert len(rows)==3
for hash_,raw in rows:
    assert hashlib.sha256(raw.encode()).hexdigest()==hash_
    assert 'PRIVATE_GRAPH_' not in raw
    execution=json.loads(raw);graph=execution['spec']
    assert graph['results']=={'kind':'callback_items','items':[0,1]}
    assert len(graph['calls']['branches'])==4
    assert len(graph['dependencies'])>=2
    assert all(node['plan']['requested_model_id']=='gpt-4o-2024-11-20' and node['plan']['target_model_id']=='gpt-4o-mini-2024-07-18' for node in graph['calls']['branches'])
    assert all([r['stable_name'] for r in node['plan']['rewrites']]==['OutputBudget'] for node in graph['calls']['branches'])
    assert execution['totals']['call_count']==4
    if execution['cost_source']=='provider_reported':assert abs(execution['totals']['cost_usd']-.08)<1e-9
    else:assert execution['cost_source']=='estimated'
profiles=conn.execute("SELECT n_observations,n_paired_observations,cost_usd_mean,cost_evidence_json FROM execution_plan_profile JOIN execution_plan_observation USING(call_site_version,execution_plan_id) WHERE execution_plan_profile.provider_protocol='provider-call-graph-runtime-v1'").fetchall()
assert len(profiles)==2 and all(row[:2]==(2,0) and abs(row[2]-.08)<1e-9 for row in profiles)
assert all(json.loads(row[3])['source']=='provider_reported' and len(json.loads(row[3])['physical_invocations'])==4 for row in profiles)
assert conn.execute('SELECT COUNT(*) FROM pragma_foreign_key_check').fetchone()[0]==0
"""
    audited = subprocess.run(
        [sys.executable, "-I", "-c", audit_script, str(tmp_path)],
        capture_output=True, text=True, timeout=30,
    )
    assert audited.returncode == 0, audited.stderr


MIXED_GRAPH_WORKER = r"""
import agentc,openai,anthropic,httpx,json
from dataclasses import asdict
sends=[]
def handler(request):
    body=json.loads(request.content);sends.append(body['model'])
    if 'claude' in body['model']:
        value={'id':'msg_graph_fixture','type':'message','role':'assistant','model':body['model'],
            'content':[{'type':'text','text':'PRIVATE_MIXED_RESULT'}],'stop_reason':'end_turn','stop_sequence':None,'usage':{'input_tokens':4,'output_tokens':2}}
    else:
        value={'id':'chatcmpl-mixed-graph-fixture','object':'chat.completion','created':0,'model':body['model'],
            'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':'PRIVATE_MIXED_RESULT'}}],'usage':{'prompt_tokens':4,'completion_tokens':2,'total_tokens':6}}
    return httpx.Response(200,json=value)
oa=openai.OpenAI(api_key='fixture',max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(handler)))
ac=anthropic.Anthropic(api_key='fixture',max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(handler)))
def task(provider):
    if provider=='openai':return oa.chat.completions.create(model='gpt-4o-mini-2024-07-18',max_tokens=128,messages=[{'role':'user','content':'PRIVATE_MIXED_PROMPT'}]).choices[0].message.content
    return ac.messages.create(model='claude-haiku-4-5-20251001',max_tokens=128,messages=[{'role':'user','content':'PRIVATE_MIXED_PROMPT'}]).content[0].text
agentc.init()
try:
    observations=[]
    with agentc.application_version('mixed-graph-excluded-originals-v1'),agentc.optimization_scope('graph-mixed-fixture',optimize=False):
        for workers in [1,2]:
            assert agentc.parallel_map(task,['openai','anthropic'],max_workers=workers,comparison='graph',on_observation=observations.append)==['PRIVATE_MIXED_RESULT']*2
        assert all(o.graph_comparison_token and o.status=='pending_billing' and o.cost_usd is None for o in observations),observations
        pair=agentc.compare_parallel_observations(*observations)
        assert pair.status=='recorded' and pair.divergence==0 and pair.acquisition_cost_usd is None,pair
        assert len(sends)==4
        print(json.dumps({'sdk_sends':len(sends),'observations':[asdict(o) for o in observations],'pair':asdict(pair)}))
finally:agentc.shutdown();oa.close();ac.close()
"""


def test_excluded_mixed_sdk_original_graphs_compare_without_planning_or_billing(tmp_path):
    env = {
        **os.environ,
        "AGENTC_STORAGE_PATH": str(tmp_path),
        "AGENTC_OPTIMIZE": "1",
        "AGENTC_EVAL_PLANNER_MODE": "best_static_joint",
        "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": "1000",
        "AGENTC_OPTIMIZE_EXPLORATION": "0",
        "AGENTC_OPTIMIZE_SHADOW": "0",
        "AGENTC_CAPTURE_CONTENT": "0",
        "AGENTC_CAPTURE_EMBEDDINGS": "0",
    }
    # The frozen planner has no declaration here. Only untouched accounting
    # capture can complete; an accidental optimization path cannot qualify.
    result = subprocess.run(
        [sys.executable, "-I", "-c", MIXED_GRAPH_WORKER], env=env,
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    proof = json.loads(result.stdout)
    assert proof["sdk_sends"] == 4
    (tmp_path / "provider-graph-mixed-sdk-proof.json").write_text(result.stdout)
    sql = r"""
import sqlite3,json,sys
from pathlib import Path
assert not any(n=='agentc' or n.startswith('agentc.') for n in sys.modules)
conn=sqlite3.connect(Path(sys.argv[1])/'cost_model.db')
rows=[json.loads(row[0]) for row in conn.execute('SELECT execution_json FROM provider_graph_execution')]
assert len(rows)==2 and all(e['cost_source']=='estimated' for e in rows)
assert all({b['runtime']['provider_protocol'] for b in e['runtime_branches']}=={'openai.chat.completions.v1','anthropic.messages.v1'} for e in rows)
assert conn.execute("SELECT COUNT(*) FROM execution_plan_profile WHERE provider_protocol='provider-call-graph-runtime-v1'").fetchone()[0]==0
assert conn.execute('SELECT COUNT(*) FROM provider_graph_pair').fetchone()[0]==1
assert conn.execute('SELECT COUNT(*) FROM provider_receipt_invocation').fetchone()[0]==4
assert not conn.execute('PRAGMA foreign_key_check').fetchall()
"""
    audit = subprocess.run(
        [sys.executable, "-I", "-c", sql, str(tmp_path)],
        capture_output=True, text=True, timeout=30,
    )
    assert audit.returncode == 0, audit.stderr


GRAPH_PAIR_WORKER = r"""
import json,time
from dataclasses import asdict
import agentc,openai,httpx
from agentc._evaluation_policy import FrozenEvaluationPolicy,evaluation_policy_scope
strong='gpt-4o-2024-11-20';cheap='gpt-4o-mini-2024-07-18'
reported=True;divergent=False;source_cap=128;sends=[]
def handler(request):
    body=json.loads(request.content);sends.append((body['model'],body.get('max_tokens')));time.sleep(.003)
    first=body['messages'][0]['content'].startswith('PRIVATE_GRAPH_PROMPT_')
    content=('PRIVATE_DIVERGENT_RESULT' if divergent and body['model']==cheap else 'PRIVATE_SHARED_RESULT') if first else 'PRIVATE_UNUSED_'+body['model']
    usage={'prompt_tokens':4,'completion_tokens':2,'total_tokens':6}
    if reported:usage.update(cost=.02,is_byok=False)
    return httpx.Response(200,json={'id':'chatcmpl-graph-pair-fixture','object':'chat.completion','created':0,
        'model':body['model'],'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':content}}],'usage':usage})
client=openai.OpenAI(api_key='fixture',max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(handler)))
def policy(target):
    return FrozenEvaluationPolicy.from_configuration(mode='best_static_joint',calibration_lock_sha256='a'*64,
        configuration={'strong_model':strong,'cheap_model':cheap,'divergence_threshold':.05,
            'static_plan':{'target_model':target,'rewrites':[] if target==strong else [{'rule':'OutputBudget','max_output_tokens':64}]},
            'router':None,'rewrite_by_target':{}})
def task(item):
    first=client.chat.completions.create(model=strong,messages=[{'role':'user','content':'PRIVATE_GRAPH_PROMPT_'+item}],max_tokens=source_cap)
    client.chat.completions.create(model=strong,messages=[{'role':'user','content':first.choices[0].message.content}],max_tokens=source_cap)
    return first.choices[0].message.content
observations=[]
def run(target,workers,items=None,template=None,pair=None):
    with evaluation_policy_scope(policy(target)),agentc.optimization_scope('graph-pair-fixture',optimize=target!=strong):
        agentc.parallel_map(task,items or ['one','two'],max_workers=workers,on_observation=observations.append,comparison='graph',graph_template_id=template,graph_pair_id=pair)
    row=observations[-1]
    assert row.graph_comparison_token and row.graph_execution_id and row.comparison_token is None,row
    return row
agentc.init()
try:
    pairs=[]
    with agentc.application_version('whole-graph-comparison-sdk-v1'),agentc.optimization_scope('graph-pair-fixture',optimize=True):
        r=run(strong,1);c=run(cheap,2)
        pair=agentc.compare_parallel_observations(r,c);assert pair.status=='recorded' and pair.divergence==0,pair
        assert pair.graph_quality['lifetime_pairs']==1 and len(pair.graph_quality['pairs'])==1
        assert pair.graph_quality['divergence_exposure']==0 and pair.graph_quality['disabled'] is None
        assert abs(pair.acquisition_cost_usd-.16)<1e-9
        retry=agentc.compare_parallel_observations(r,c);assert retry.status=='already_recorded' and retry.pair_id==pair.pair_id
        assert retry.graph_quality['lifetime_pairs']==1
        pairs.append(asdict(pair))
        divergent=True
        r2=run(strong,1);c2=run(cheap,2)
        pair=agentc.compare_parallel_observations(r2,c2);assert pair.status=='recorded' and pair.divergence==1,pair
        assert pair.graph_quality['lifetime_pairs']==2 and pair.graph_quality['disabled']
        assert pair.graph_quality['divergence_exposure']==1 and pair.graph_quality['pairs']==[]
        pairs.append(asdict(pair))
        assert agentc.compare_parallel_observations(r,c2).status=='abstained'
        divergent=False;reported=False
        r3=run(strong,1);c3=run(cheap,2)
        assert r3.status==c3.status=='pending_billing' and r3.cost_usd is c3.cost_usd is None
        pair=agentc.compare_parallel_observations(r3,c3);assert pair.status=='recorded' and pair.divergence==0 and pair.acquisition_cost_usd is None,pair
        assert pair.graph_quality['lifetime_pairs']==3 and pair.graph_quality['disabled']
        assert pair.graph_quality['divergence_exposure']==1
        pairs.append(asdict(pair))
        mismatch=run(cheap,2,['two','one'])
        assert agentc.compare_parallel_observations(r3,mismatch).status=='abstained'
        r4=run(strong,1);source_cap=256;c4=run(cheap,2)
        assert agentc.compare_parallel_observations(r4,c4).status=='abstained'
    assert len(sends)==36
    assert all(model==strong and cap==128 for model,cap in sends[:4])
    assert all(model==cheap and cap==64 for model,cap in sends[4:8])
    print(json.dumps({'sdk_sends':len(sends),'observations':[asdict(v) for v in observations],'pairs':pairs}))
finally:agentc.shutdown();client.close()
"""


GRAPH_PREPARATION_WORKER = GRAPH_PAIR_WORKER.split("\nagentc.init()", 1)[0] + r"""
import anthropic,asyncio
ac=anthropic.Anthropic(api_key='fixture',max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(handler)))
async def async_stream():
    client=anthropic.AsyncAnthropic(api_key='fixture',max_retries=0,http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        async with client.messages.stream(model='claude-haiku-4-5-20251001',max_tokens=128,messages=[{'role':'user','content':'PRIVATE_STREAM_PROMPT'}]) as stream:
            return (await stream.get_final_message()).content[0].text
    finally:await client.close()
original_task=task;streaming=False
def task(item):
    if streaming=='async':return asyncio.run(async_stream())
    if streaming:
        with ac.messages.stream(model='claude-haiku-4-5-20251001',max_tokens=128,messages=[{'role':'user','content':'PRIVATE_STREAM_PROMPT'}]) as stream:
            return stream.get_final_message().content[0].text
    return original_task(item)
agentc.init()
try:
    with agentc.application_version('whole-graph-preparation-sdk-v1'):
        original=run(cheap,2)
        prepared=run(cheap,2,template=original.graph_execution_id)
        assert prepared.execution_plan_id==original.execution_plan_id
        assert prepared.graph_execution_id!=original.graph_execution_id
        assert len(sends)==8
        def rejects(action):
            before=len(sends)
            try:action()
            except (RuntimeError,ValueError):pass
            else:raise AssertionError('prepared graph should have raised before HTTP')
            assert len(sends)==before,(before,sends)
        source_cap=256
        rejects(lambda:run(cheap,2,template=original.graph_execution_id))
        source_cap=128
        rejects(lambda:run(strong,2,template=original.graph_execution_id))
        rejects(lambda:run(cheap,1,template=original.graph_execution_id))
        rejects(lambda:run(cheap,2,template='f'*64))
        for streaming in ['sync','async']:
            rejects(lambda:run(cheap,2,template=original.graph_execution_id))
        streaming=False
        with agentc.application_version('other-application-v1'):
            rejects(lambda:run(cheap,2,template=original.graph_execution_id))
    rejects(lambda:run(cheap,2,template=original.graph_execution_id))
    print(json.dumps({'sdk_sends':len(sends),'observations':[asdict(v) for v in observations if v.graph_execution_id]}))
finally:agentc.shutdown();client.close();ac.close()
"""


GRAPH_COHORT_WORKER = GRAPH_PAIR_WORKER.split("\nagentc.init()", 1)[0] + r"""
import os,subprocess,sys,threading
armed=False;changed=False;lock=threading.Lock();reference_key=None
base_handler=handler
def handler(request):
    global changed
    response=base_handler(request)
    with lock:
        if armed and not changed:
            # The SDK worker never imports SQLite. A separate interpreter
            # simulates retirement of the native reference-price projection
            # while the candidate's first physical requests are in flight.
            value=subprocess.run([sys.executable,'-I','-c',r'''
import sqlite3,json,os,sys
from pathlib import Path
assert not any(n=='agentc' or n.startswith('agentc.') for n in sys.modules)
conn=sqlite3.connect(Path(os.environ['AGENTC_STORAGE_PATH'])/'cost_model.db')
key=json.loads(sys.argv[1])
args=(key['call_site_version'],key['execution_plan_id'])
raw,=conn.execute('SELECT runtime_json FROM provider_graph_execution_state WHERE call_site_version=? AND execution_plan_id=?',args).fetchone()
runtime=json.loads(raw);runtime['price_table_version']='retired-reference-price-fixture'
conn.execute('UPDATE provider_graph_execution_state SET runtime_json=? WHERE call_site_version=? AND execution_plan_id=?',(json.dumps(runtime,sort_keys=True,separators=(',',':')),)+args)
conn.commit();conn.close()
''',json.dumps(reference_key)],capture_output=True,text=True,timeout=30)
            assert value.returncode==0,value.stderr
            changed=True
    return response
client.close()
client=openai.OpenAI(api_key='fixture',max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(handler)))
agentc.init()
try:
    with agentc.application_version('shared-graph-cohort-sdk-v1'):
        r=run(strong,1);c=run(cheap,2)
        bootstrap=agentc.compare_parallel_observations(r,c)
        assert bootstrap.status=='recorded' and bootstrap.graph_quality['prepared_pairs']==[]
        rr=run(strong,1,template=r.graph_execution_id,pair=bootstrap.pair_id)
        cc=run(cheap,2,template=c.graph_execution_id,pair=bootstrap.pair_id)
        pair=agentc.compare_parallel_observations(rr,cc)
        assert pair.status=='recorded' and pair.divergence==0
        assert len(pair.graph_quality['prepared_pairs'])==1 and pair.graph_quality['prepared_divergence_upper_p95']==0
        assert len(sends)==16
        before=len(sends)
        try:run(cheap,2,template=c.graph_execution_id,pair=pair.pair_id)
        except RuntimeError:pass
        else:raise AssertionError('template is not a member of this pair')
        assert len(sends)==before
        reference_key=bootstrap.graph_quality['reference_key']
        armed=True
        try:run(cheap,2,template=c.graph_execution_id,pair=bootstrap.pair_id)
        except RuntimeError:pass
        else:raise AssertionError('reference retirement should hold the next candidate dispatch')
        assert changed and 1<=len(sends)-before<=2,sends
        assert observations[-1].graph_execution_id is None,observations[-1]
        retried=agentc.compare_parallel_observations(rr,cc)
        assert retried.status=='already_recorded' and retried.pair_id==pair.pair_id and retried.graph_quality is None
    print(json.dumps({'sdk_sends':len(sends),'observations':[asdict(v) for v in observations],
        'bootstrap':asdict(bootstrap),'pair':asdict(pair),'retired_pair':asdict(retried)}))
finally:agentc.shutdown();client.close()
"""


def test_shared_graph_cohort_binds_both_sdk_legs_and_holds_inflight_reference_retirement(tmp_path):
    env = {
        **os.environ,
        "AGENTC_STORAGE_PATH": str(tmp_path),
        "AGENTC_OPTIMIZE": "1",
        "AGENTC_EVAL_PLANNER_MODE": "best_static_joint",
        "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": "1000",
        "AGENTC_OPTIMIZE_EXPLORATION": "0",
        "AGENTC_OPTIMIZE_SHADOW": "0",
        "AGENTC_CAPTURE_CONTENT": "0",
        "AGENTC_CAPTURE_EMBEDDINGS": "0",
    }
    result = subprocess.run(
        [sys.executable, "-I", "-c", GRAPH_COHORT_WORKER], env=env,
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    proof = json.loads(result.stdout)
    assert 17 <= proof["sdk_sends"] <= 18
    (tmp_path / "provider-graph-cohort-sdk-proof.json").write_text(result.stdout)
    sql = r"""
import sqlite3,json,hashlib,sys
from pathlib import Path
assert not any(n=='agentc' or n.startswith('agentc.') for n in sys.modules)
conn=sqlite3.connect(Path(sys.argv[1])/'cost_model.db')
canon=lambda v:json.dumps(v,sort_keys=True,separators=(',',':'),ensure_ascii=False)
contexts=[]
for raw,bound in conn.execute('SELECT lease_json,execution_id FROM provider_graph_preparation'):
    lease=json.loads(raw);ctx=lease.get('comparison_context')
    if ctx is None:continue
    assert 'PRIVATE_' not in raw
    assert hashlib.sha256(canon(ctx['generation']).encode()).hexdigest()==ctx['generation_id']
    pair_hash,pair_raw=conn.execute('SELECT pair_sha256,pair_json FROM provider_graph_pair WHERE pair_id=?',(ctx['source_pair_id'],)).fetchone()
    assert hashlib.sha256(pair_raw.encode()).hexdigest()==pair_hash==ctx['source_pair_sha256']
    if bound is not None:contexts.append(ctx)
    else:
        assert ctx['role']=='candidate'
        calls=conn.execute('SELECT completion_json FROM provider_receipt_invocation WHERE batch_id=?',(lease['batch_id'],)).fetchall()
        assert 1<=len(calls)<=2 and all(json.loads(raw[0])['status']=='completed' for raw in calls)
assert len(contexts)==2 and {c['role'] for c in contexts}=={'reference','candidate'}
assert contexts[0]['generation']==contexts[1]['generation']
assert contexts[0]['generation_id']==contexts[1]['generation_id']
assert conn.execute('SELECT COUNT(*) FROM provider_graph_execution').fetchone()[0]==4
assert conn.execute('SELECT COUNT(*) FROM provider_graph_pair').fetchone()[0]==2
assert conn.execute('SELECT COUNT(*) FROM provider_graph_quality_event').fetchone()[0]==2
assert not conn.execute('PRAGMA foreign_key_check').fetchall()
"""
    audited = subprocess.run(
        [sys.executable, "-I", "-c", sql, str(tmp_path)],
        capture_output=True, text=True, timeout=30,
    )
    assert audited.returncode == 0, audited.stderr


GRAPH_ACQUISITION_WORKER = r"""
import json,threading,contextvars
from dataclasses import asdict
import agentc,openai,httpx
from agentc._evaluation_policy import FrozenEvaluationPolicy,evaluation_policy_scope
from agentc._dispatch_context import provider_dispatch
from agentc._optimization_scope import optimization_scope
from agentc._provider_receipts import instrument_provider
calls=[];fail=True
strong='gpt-4o-2024-11-20';cheap='gpt-4o-mini-2024-07-18'
def handler(request):
    body=json.loads(request.content);calls.append(body)
    if fail and body['model']==cheap:return httpx.Response(500,json={'error':{'message':'fixture','type':'server_error'}})
    return httpx.Response(200,json={'id':'fixture','object':'chat.completion','created':0,'model':body['model'],
        'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':'PRIVATE_ACQUISITION_RESULT'}}],
        'usage':{'prompt_tokens':2,'completion_tokens':1,'total_tokens':3,'cost':0.01,'is_byok':False}})
client=openai.OpenAI(api_key='fixture',max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(handler)))
policy=FrozenEvaluationPolicy.from_configuration(mode='best_static_joint',calibration_lock_sha256='b'*64,
    configuration={'strong_model':strong,'cheap_model':cheap,'divergence_threshold':.05,'static_plan':{'target_model':cheap,'rewrites':[{'rule':'OutputBudget','max_output_tokens':128}]},'router':None,'rewrite_by_target':{}})
agentc.init()
try:
    observations=[]
    with agentc.application_version('graph-acquisition-fallback-sdk-v1'),evaluation_policy_scope(policy):
        result=agentc.parallel_map(lambda _:client.chat.completions.create(model=strong,messages=[{'role':'user','content':'PRIVATE_ACQUISITION_PROMPT'}],max_tokens=1024),[0],max_workers=1,comparison='graph',on_observation=observations.append)
    fallback=observations[-1];value=fallback.graph_acquisition
    assert fallback.graph_execution_id is None and value,value
    assert value['physical_invocations']==2 and value['roles']=={'primary':1,'fallback':1}
    assert value['known_cost_usd']==.01 and value['unknown_cost_invocations']==1 and not value['cost_complete']
    from agentc._batch_observation import BatchObservation,note_sdk_call
    from agentc._optimizer import plan_call
    from agentc._patches._optimizer_glue import build_call_dict_openai
    batch=BatchObservation(application_version='a'*64,callback_version='graph-auxiliary-sdk-v1',item_count=1,max_workers=1,graph_comparison_requested=True)
    assert batch.graph_preparation_id
    fail=False
    body={'model':strong,'messages':[{'role':'user','content':'PRIVATE_ACQUISITION_AUXILIARY'}],'max_tokens':8}
    with batch.item(0),agentc.application_version('graph-auxiliary-sdk-v1'):
        note_sdk_call()
        source=build_call_dict_openai(body,call_site_id='graph-auxiliary',trace_id_hex='0'*32,span_id_hex='0'*16)
        with optimization_scope('original',optimize=False):plan=plan_call(source)
        provider_call=instrument_provider(client.chat.completions.create.__wrapped__,client.chat.completions,'openai.chat.completions.v1')
        def invoke(role):
            with provider_dispatch(role,plan=plan,primary_plan=plan):return provider_call(**body)
        invoke('primary');invoke('validation');invoke('shadow')
        context=contextvars.copy_context()
    batch.provider_receipts.seal([1]);batch.provider_receipts.finish_economics(10.)
    before=batch.provider_receipts.acquisition_report()
    assert before['cost_complete'] and before['physical_invocations']==3
    context.run(invoke,'exploration')
    after=batch.provider_receipts.acquisition_report()
    assert after['cost_complete'] and after['physical_invocations']==4 and after['late_invocations']==1
    assert after['roles']=={'primary':1,'validation':1,'shadow':1,'exploration':1}
    assert abs(after['known_cost_usd']-.04)<1e-12 and after['late_dispatch_latency_ms']>0
    assert len(calls)==6
    print(json.dumps({'sdk_sends':len(calls),'fallback':asdict(fallback),'auxiliary_batch_id':batch.provider_receipts.batch_id,'auxiliary':after}))
finally:agentc.shutdown();client.close()
"""


GRAPH_ROLE_WORKER = r"""
import json,time
import agentc,openai,httpx
from agentc._optimizer import plan_call
from agentc._patches._optimizer_glue import build_call_dict_openai
from agentc._provider_receipts import ProviderReceiptBatch,instrument_provider,bind_plan,note_logical_call,_request_fingerprint,_transport_fingerprint
from agentc._dispatch_context import provider_dispatch
from agentc._batch_observation import comparison_fingerprint,GRAPH_COMPARISON_VERSION
calls=[];failing=False
cheap='gpt-4o-mini-2024-07-18';strong='gpt-4o-2024-11-20';protocol='openai.chat.completions.v1'
def handler(request):
    body=json.loads(request.content);calls.append(body)
    if failing and body['model']==cheap and body['messages'][0]['content']=='PRIVATE_ROLE_PRIMARY':
        return httpx.Response(500,json={'error':{'message':'fixture','type':'server_error'}})
    return httpx.Response(200,json={'id':'fixture','object':'chat.completion','created':0,'model':body['model'],
        'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':'PRIVATE_ROLE_RESULT'}}],
        'usage':{'prompt_tokens':2,'completion_tokens':1,'total_tokens':3,'cost':.01,'is_byok':False}})
client=openai.OpenAI(api_key='fixture',max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(handler)))
agentc.init()
try:
    plans={};nodes={};bodies={}
    with agentc.application_version('physical-role-sdk-v1'):
        for name,model,logical in [('primary',cheap,0),('fallback',strong,0),('validation',strong,0),('shadow',strong,0),('exploration',strong,0),('consumer',cheap,1)]:
            body={'model':model,'messages':[{'role':'user','content':'PRIVATE_ROLE_'+name.upper()}],'max_tokens':8};bodies[name]=body
            source=build_call_dict_openai(body,call_site_id='role-'+name,trace_id_hex='0'*32,span_id_hex=format(len(plans)+1,'016x'),client=client)
            plan=plan_call(source,observation_only=True);plans[name]=plan
            response=json.loads(agentc._native.optimize_describe_provider_graph_node(json.dumps({'item':0,'call':logical}),plan.source_call_json,plan.raw_json))
            assert response['status']=='recorded',response
            assert response['admission']=='descriptor_only'
            nodes[name]=response['node']
    graph={'version':'provider-call-graph-v1','calls':{'application_version':json.loads(plans['primary'].source_call_json)['parameters']['extra']['agentc_route_context']['application_version']['sha256'],
        'callback_version':'physical-role-sdk-v1','item_count':1,'max_workers':1,'branches':[
            {'position':nodes[name]['position'],'call_site_version':nodes[name]['key']['call_site_version'],'plan':nodes[name]['plan']} for name in ['primary','consumer']]},
        'dependencies':[{'producer':{'item':0,'call':0},'consumer':{'item':0,'call':1}}],
        'results':{'kind':'callback_items','items':[0]}}
    dispatches=[];bindings=[]
    for ordinal,name in enumerate(['primary','fallback','validation','shadow','exploration','consumer']):
        slot={'position':nodes[name]['position'],'ordinal':0 if name=='consumer' else ordinal}
        dispatches.append({'slot':slot,'role':'primary' if name=='consumer' else name,'call_site_version':nodes[name]['key']['call_site_version'],
            'plan':nodes[name]['plan'],'activation':{'kind':'on_failure','predecessor':{'position':nodes['primary']['position'],'ordinal':0}} if name=='fallback' else {'kind':'always'}})
        bindings.append({'slot':slot,'runtime':nodes[name]['runtime'],'request_sha256':_request_fingerprint(bodies[name],protocol),
            'transport_sha256':_transport_fingerprint(client.chat.completions,protocol)})
    prepared={'program':{'version':'provider-role-graph-v1','graph':graph,'dispatches':dispatches},'bindings':bindings,
        'logical_requests':[{'position':nodes[name]['position'],'sha256':nodes[name]['logical_request_sha256']} for name in ['primary','consumer']]}
    input_hash=comparison_fingerprint([0],version=GRAPH_COMPARISON_VERSION)
    def batch():
        b=ProviderReceiptBatch(graph['calls']['application_version'],'physical-role-sdk-v1',1)
        b.prepare_role_graph(prepared,input_hash)
        return b
    def invoke(b,name,*,body=None,physical=None,kind=None):
        owner=plans['consumer' if name=='consumer' else 'primary']
        with b.item(0):
            note_logical_call(1 if name=='consumer' else 0);bind_plan(owner)
            provider_call=instrument_provider(client.chat.completions.create.__wrapped__,client.chat.completions,protocol)
            with provider_dispatch(kind or ('primary' if name=='consumer' else name),plan=owner,primary_plan=owner,
                dispatch_plan=physical or plans[name]):
                return provider_call(**(body or bodies[name]))
    executions=[]
    for should_fail in [True,False]:
        failing=should_fail;b=batch();start=time.perf_counter()
        if should_fail:
            try:invoke(b,'primary')
            except openai.InternalServerError:pass
            else:raise AssertionError('failed primary should propagate to the explicit driver')
            result=invoke(b,'fallback')
        else:result=invoke(b,'primary')
        for name in ['validation','shadow','exploration']:invoke(b,name)
        result=invoke(b,'consumer');b.seal([2]);b.finish_economics((time.perf_counter()-start)*1000)
        comparison={'version':GRAPH_COMPARISON_VERSION,'input_sha256':input_hash,'output_sha256':comparison_fingerprint([result.choices[0].message.content],version=GRAPH_COMPARISON_VERSION)}
        command={'operation':'finish_role_graph','lease_id':b.graph_preparation_id,'comparison':comparison}
        value=json.loads(agentc._native.optimize_record_provider_receipt(json.dumps(command)))
        assert value['status']=='recorded' and value['admission']=='acquisition_only',value
        e=value['execution'];executions.append(e)
        assert e['selected_results'][0]['slot']['ordinal']==(1 if should_fail else 0)
        assert e['acquisition']['physical_invocations']==(6 if should_fail else 5)
        assert e['acquisition']['cost_complete']==(not should_fail)
        retry=json.loads(agentc._native.optimize_record_provider_receipt(json.dumps(command)))
        assert retry['execution']==e
    # Each negative control starts a fresh program. A rejected physical call
    # aborts that acquisition; its consumed SDK ordinal cannot be reused.
    rejected=[];failing=False
    for fault in ['request','plan','dependency','role']:
        b=batch();before=len(calls)
        try:
            if fault=='request':invoke(b,'primary',body={**bodies['primary'],'max_tokens':9})
            elif fault=='plan':invoke(b,'primary',physical=plans['fallback'])
            elif fault=='dependency':invoke(b,'consumer')
            else:invoke(b,'primary',kind='validation')
        except RuntimeError:pass
        else:raise AssertionError('changed physical graph dispatched: '+fault)
        assert len(calls)==before
        rejected.append({'fault':fault,'batch_id':b.batch_id})
    assert len(calls)==11
    print(json.dumps({'sdk_sends':len(calls),'executions':executions,'rejected':rejected}))
finally:agentc.shutdown();client.close()
"""


def test_prepared_role_graph_sdk_binds_fallback_auxiliary_work_and_result_selection(tmp_path):
    env = {
        **os.environ,
        "AGENTC_STORAGE_PATH": str(tmp_path),
        "AGENTC_OPTIMIZE": "1",
        "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": "1000",
        "AGENTC_OPTIMIZE_EXPLORATION": "0",
        "AGENTC_OPTIMIZE_SHADOW": "0",
        "AGENTC_CAPTURE_CONTENT": "0",
        "AGENTC_CAPTURE_EMBEDDINGS": "0",
    }
    result = subprocess.run(
        [sys.executable, "-I", "-c", GRAPH_ROLE_WORKER], env=env,
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    proof = json.loads(result.stdout)
    assert proof["sdk_sends"] == 11
    (tmp_path / "provider-graph-role-sdk-proof.json").write_text(result.stdout)
    restart = subprocess.run(
        [sys.executable, "-I", "-c", r"""
import agentc,json,sys
agentc.init()
try:
    proof=json.loads(sys.argv[1])
    for original in proof['executions']:
        result=json.loads(agentc._native.optimize_record_provider_receipt(json.dumps({'operation':'role_graph_execution','execution_id':original['execution_id']})))
        assert result['status']=='recorded' and result['admission']=='acquisition_only',result
        assert result['execution']==original
    print('role executions matched original custody after SDK restart')
finally:agentc.shutdown()
""", result.stdout], env=env, capture_output=True, text=True, timeout=60,
    )
    assert restart.returncode == 0, restart.stderr
    sql = r"""
import sqlite3,json,hashlib,sys
from pathlib import Path
assert not any(n=='agentc' or n.startswith('agentc.') for n in sys.modules)
conn=sqlite3.connect(Path(sys.argv[1])/'cost_model.db')
assert conn.execute('SELECT COUNT(*) FROM provider_graph_role_preparation').fetchone()[0]==6
assert conn.execute('SELECT COUNT(*) FROM provider_graph_role_execution').fetchone()[0]==2
assert conn.execute('SELECT COUNT(*) FROM provider_receipt_invocation').fetchone()[0]==11
assert conn.execute('SELECT COUNT(*) FROM provider_graph_execution').fetchone()[0]==0
for hash_,raw in conn.execute('SELECT prepared_sha256,prepared_json FROM provider_graph_role_preparation'):
    assert hashlib.sha256(raw.encode()).hexdigest()==hash_ and 'PRIVATE_' not in raw
for hash_,raw in conn.execute('SELECT execution_sha256,execution_json FROM provider_graph_role_execution'):
    assert hashlib.sha256(raw.encode()).hexdigest()==hash_ and 'PRIVATE_' not in raw
assert not conn.execute('PRAGMA foreign_key_check').fetchall()
"""
    audit = subprocess.run(
        [sys.executable, "-I", "-c", sql, str(tmp_path)], capture_output=True,
        text=True, timeout=30,
    )
    assert audit.returncode == 0, audit.stderr


def test_graph_acquisition_sdk_accounts_failed_fallback_all_roles_and_late_work(tmp_path):
    env = {
        **os.environ,
        "AGENTC_STORAGE_PATH": str(tmp_path),
        "AGENTC_OPTIMIZE": "1",
        "AGENTC_EVAL_PLANNER_MODE": "best_static_joint",
        "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": "1000",
        "AGENTC_OPTIMIZE_EXPLORATION": "0",
        "AGENTC_OPTIMIZE_SHADOW": "0",
        "AGENTC_CAPTURE_CONTENT": "0",
        "AGENTC_CAPTURE_EMBEDDINGS": "0",
    }
    result = subprocess.run(
        [sys.executable, "-I", "-c", GRAPH_ACQUISITION_WORKER], env=env,
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    proof = json.loads(result.stdout)
    assert proof["sdk_sends"] == 6
    (tmp_path / "provider-graph-acquisition-sdk-proof.json").write_text(result.stdout)
    restart = subprocess.run(
        [sys.executable, "-I", "-c", r"""
import agentc,json,sys
agentc.init()
try:
    proof=json.loads(sys.argv[1])
    for original in [proof['fallback']['graph_acquisition'],proof['auxiliary']]:
        result=json.loads(agentc._native.optimize_record_provider_receipt(json.dumps({'operation':'graph_acquisition','driver_id':original['driver_id']})))
        assert result['acquisition']==original,(result,original)
    print('restart matched original lifetime costs')
finally:agentc.shutdown()
""", result.stdout], env=env, capture_output=True, text=True, timeout=60,
    )
    assert restart.returncode == 0, restart.stderr
    sql = r"""
import sqlite3,json,hashlib,sys
from pathlib import Path
assert not any(n=='agentc' or n.startswith('agentc.') for n in sys.modules)
conn=sqlite3.connect(Path(sys.argv[1])/'cost_model.db')
assert conn.execute('SELECT COUNT(*) FROM provider_graph_acquisition').fetchone()[0]==2
assert conn.execute('SELECT COUNT(*) FROM provider_graph_acquisition_invocation').fetchone()[0]==6
assert conn.execute('SELECT COUNT(*) FROM provider_graph_execution').fetchone()[0]==0
roles={}
for hash_,raw in conn.execute('SELECT charge_sha256,charge_json FROM provider_graph_acquisition_invocation'):
    assert hashlib.sha256(raw.encode()).hexdigest()==hash_ and 'PRIVATE_' not in raw
    c=json.loads(raw);roles[c['role']]=roles.get(c['role'],0)+1
assert roles=={'primary':2,'fallback':1,'validation':1,'shadow':1,'exploration':1}
assert not conn.execute('PRAGMA foreign_key_check').fetchall()
"""
    audit = subprocess.run(
        [sys.executable, "-I", "-c", sql, str(tmp_path)], capture_output=True,
        text=True, timeout=30,
    )
    assert audit.returncode == 0, audit.stderr


def test_prepared_graph_rejects_changed_sdk_nodes_before_http_and_keeps_original_start(tmp_path):
    env = {
        **os.environ,
        "AGENTC_STORAGE_PATH": str(tmp_path),
        "AGENTC_OPTIMIZE": "1",
        "AGENTC_EVAL_PLANNER_MODE": "best_static_joint",
        "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": "1000",
        "AGENTC_OPTIMIZE_EXPLORATION": "0",
        "AGENTC_OPTIMIZE_SHADOW": "0",
        "AGENTC_CAPTURE_CONTENT": "0",
        "AGENTC_CAPTURE_EMBEDDINGS": "0",
    }
    result = subprocess.run(
        [sys.executable, "-I", "-c", GRAPH_PREPARATION_WORKER], env=env,
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    proof = json.loads(result.stdout)
    assert proof["sdk_sends"] == 8 and len(proof["observations"]) == 2
    (tmp_path / "provider-graph-preparation-sdk-proof.json").write_text(result.stdout)
    sql = r"""
import sqlite3,json,hashlib,sys
from pathlib import Path
assert not any(n=='agentc' or n.startswith('agentc.') for n in sys.modules)
conn=sqlite3.connect(Path(sys.argv[1])/'cost_model.db')
canon=lambda value:json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False)
executions=[json.loads(row[0]) for row in conn.execute('SELECT execution_json FROM provider_graph_execution ORDER BY rowid')]
assert len(executions)==2
for e in executions:
    raw,hash_,bound=conn.execute('SELECT lease_json,lease_sha256,execution_id FROM provider_graph_preparation WHERE lease_id=?',(e['preparation_id'],)).fetchone()
    lease=json.loads(raw);assert 'PRIVATE_' not in raw
    assert hashlib.sha256(raw.encode()).hexdigest()==hash_
    identity={k:v for k,v in lease.items() if k!='lease_id'}
    assert hashlib.sha256(canon(identity).encode()).hexdigest()==lease['lease_id']
    assert bound==e['execution_id'] and lease['batch_id']==e['batch_id']
    assert 0<=lease['started_at_us']<=e['observed_at_us']
    assert conn.execute('SELECT COUNT(*) FROM provider_receipt_invocation WHERE batch_id=?',(e['batch_id'],)).fetchone()[0]==4
    if lease['template'] is None:
        assert e==executions[0]
    else:
        assert lease['template']['execution_id']==executions[0]['execution_id']
        assert lease['template']['spec']==e['spec'] and lease['template']['runtime_branches']==e['runtime_branches']
        nodes=conn.execute('SELECT node_sha256,node_json FROM provider_graph_prepared_node WHERE lease_id=?',(lease['lease_id'],)).fetchall()
        assert len(nodes)==4
        assert all(hashlib.sha256(raw.encode()).hexdigest()==h and 'PRIVATE_' not in raw for h,raw in nodes)
assert conn.execute('SELECT COUNT(*) FROM provider_receipt_invocation').fetchone()[0]==8
assert conn.execute('SELECT COUNT(*) FROM provider_graph_preparation WHERE execution_id IS NULL').fetchone()[0]==4
assert not conn.execute('PRAGMA foreign_key_check').fetchall()
"""
    audited = subprocess.run(
        [sys.executable, "-I", "-c", sql, str(tmp_path)],
        capture_output=True, text=True, timeout=30,
    )
    assert audited.returncode == 0, audited.stderr


def test_real_sdk_whole_graph_results_pair_once_with_actual_baseline_and_candidate(tmp_path):
    env = {
        **os.environ,
        "AGENTC_STORAGE_PATH": str(tmp_path),
        "AGENTC_OPTIMIZE": "1",
        "AGENTC_EVAL_PLANNER_MODE": "best_static_joint",
        "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": "1000",
        "AGENTC_OPTIMIZE_EXPLORATION": "0",
        "AGENTC_OPTIMIZE_SHADOW": "0",
        "AGENTC_CAPTURE_CONTENT": "0",
        "AGENTC_CAPTURE_EMBEDDINGS": "0",
    }
    result = subprocess.run(
        [sys.executable, "-I", "-c", GRAPH_PAIR_WORKER], env=env,
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    proof = json.loads(result.stdout)
    (tmp_path / "provider-graph-pair-sdk-proof.json").write_text(result.stdout)
    assert proof["sdk_sends"] == 36 and len(proof["pairs"]) == 3
    restarted = subprocess.run(
        [sys.executable, "-I", "-c", r"""
import agentc,json,sys
agentc.init()
try:
    rows=json.loads(sys.argv[1])
    value=json.loads(agentc._native.optimize_compare_provider_graph(rows[0]['graph_comparison_token'],rows[1]['graph_comparison_token']))
    assert value['status']=='already_recorded' and value['divergence']==0
    assert value['quality']['lifetime_pairs']==3 and value['quality']['disabled']
    assert value['quality']['divergence_exposure']==1
    print(json.dumps(value))
finally:agentc.shutdown()
""", json.dumps(proof["observations"])], env=env,
        capture_output=True, text=True, timeout=60,
    )
    assert restarted.returncode == 0, restarted.stderr
    assert json.loads(restarted.stdout)["pair_id"] == proof["pairs"][0]["pair_id"]
    (tmp_path / "provider-graph-pair-sdk-restart.json").write_text(restarted.stdout)
    sql = r"""
import json,sqlite3,hashlib,sys
from pathlib import Path
assert not any(n=='agentc' or n.startswith('agentc.') for n in sys.modules)
conn=sqlite3.connect(Path(sys.argv[1])/'cost_model.db')
executions={}
for identity,hash_,raw in conn.execute('SELECT execution_id,execution_sha256,execution_json FROM provider_graph_execution'):
    assert hashlib.sha256(raw.encode()).hexdigest()==hash_ and 'PRIVATE_' not in raw
    e=json.loads(raw);assert e['comparison']['version']=='exact-provider-graph-results-v1'
    assert len(e['runtime_branches'])==4
    executions[identity]=e
assert len(executions)==9
for hash_,raw in conn.execute('SELECT pair_sha256,pair_json FROM provider_graph_pair'):
    assert hashlib.sha256(raw.encode()).hexdigest()==hash_
    p=json.loads(raw);r=executions[p['reference_execution_id']];c=executions[p['candidate_execution_id']]
    assert r['spec']['calls']['max_workers']==1
    assert all(b['plan']['target_model_id']==b['plan']['requested_model_id'] and not b['plan']['rewrites'] for b in r['spec']['calls']['branches'])
    assert p['reference_runtime']==r['runtime'] and p['runtime_version']==c['runtime']
    assert p['divergence']==float(r['comparison']['output_sha256']!=c['comparison']['output_sha256'])
    assert r['comparison']['input_sha256']==c['comparison']['input_sha256']
assert conn.execute('SELECT COUNT(*) FROM provider_graph_pair').fetchone()[0]==3
assert conn.execute('SELECT COUNT(*) FROM provider_graph_quality_event').fetchone()[0]==3
assert conn.execute('SELECT SUM(lifetime_pairs) FROM provider_graph_quality_state').fetchone()[0]==3
assert conn.execute('SELECT SUM(n_paired_observations) FROM execution_plan_profile').fetchone()[0]==0
assert conn.execute('SELECT COUNT(*) FROM provider_receipt_invocation').fetchone()[0]==36
assert not conn.execute('PRAGMA foreign_key_check').fetchall()
"""
    audited = subprocess.run(
        [sys.executable, "-I", "-c", sql, str(tmp_path)],
        capture_output=True, text=True, timeout=30,
    )
    assert audited.returncode == 0, audited.stderr


GRAPH_ROLE_QUALITY_WORKER = r"""

import json,time,os
import agentc,openai,httpx
from agentc._optimizer import plan_call
from agentc._patches._optimizer_glue import build_call_dict_openai
from agentc._provider_receipts import ProviderReceiptBatch,instrument_provider,bind_plan,note_logical_call,_request_fingerprint,_transport_fingerprint
from agentc._dispatch_context import provider_dispatch
from agentc._batch_observation import comparison_fingerprint,GRAPH_COMPARISON_VERSION
calls=[];failing=False;adverse=False;metadata=[];generations={}
financial_mode=os.environ.get('AGENTC_GRAPH_FINANCIAL_TEST') in {'1','2'}
billing_mode=os.environ.get('AGENTC_GRAPH_FINANCIAL_TEST')=='2'
cheap='openai/gpt-4o-mini' if billing_mode else 'gpt-4o-mini-2024-07-18'
strong='openai/gpt-4o' if billing_mode else 'gpt-4o-2024-11-20'
protocol='openai.chat.completions.v1'
def handler(request):
    if request.method=='GET':
        generation=request.url.params['id'];metadata.append(generation)
        return httpx.Response(200,json={'data':{'id':generation,'request_id':'req-'+generation,'model':generations[generation],
            'native_tokens_prompt':2,'native_tokens_completion':1,'total_cost':.02,'is_byok':False,'cancelled':False,'finish_reason':'stop'}})
    body=json.loads(request.content);calls.append(body)
    if failing and body['model']==cheap and body['messages'][0]['content']=='PRIVATE_ROLE_PRIMARY':
        return httpx.Response(500,json={'error':{'message':'fixture','type':'server_error'}})
    generation='gen-role-financial-'+str(len(calls)) if billing_mode else 'fixture'
    if billing_mode:generations[generation]=body['model']
    usage={'prompt_tokens':2,'completion_tokens':1,'total_tokens':3,'prompt_tokens_details':{'cached_tokens':0}}
    if not financial_mode:usage.update(cost=.01,is_byok=False)
    return httpx.Response(200,headers={'x-request-id':'req-'+generation},json={'id':generation,'object':'chat.completion','created':0,'model':body['model'],
        'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':('PRIVATE_ROLE_BAD' if adverse and body['messages'][0]['content']=='PRIVATE_ROLE_CONSUMER' else 'PRIVATE_ROLE_RESULT')}}],
        'service_tier':'default','usage':usage})
client=openai.OpenAI(api_key='fixture',base_url='https://openrouter.ai/api/v1' if billing_mode else 'https://api.openai.com/v1',max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(handler)))
agentc.init()
tariff_scope=agentc.tariff_accounting() if financial_mode and not billing_mode else None
if tariff_scope:tariff_scope.__enter__()
try:
    plans={};nodes={};bodies={}
    with agentc.application_version('physical-role-sdk-v1'):
        for name,model,logical in [('primary',cheap,0),('fallback',strong,0),('validation',strong,0),('shadow',strong,0),('exploration',strong,0),('consumer',cheap,1)]:
            body={'model':model,'messages':[{'role':'user','content':'PRIVATE_ROLE_'+name.upper()}],'max_tokens':8};bodies[name]=body
            source=build_call_dict_openai(body,call_site_id='role-'+name,trace_id_hex='0'*32,span_id_hex=format(len(plans)+1,'016x'),client=client)
            plan=plan_call(source,observation_only=True);plans[name]=plan
            response=json.loads(agentc._native.optimize_describe_provider_graph_node(json.dumps({'item':0,'call':logical}),plan.source_call_json,plan.raw_json))
            assert response['status']=='recorded',response
            assert response['admission']=='descriptor_only'
            nodes[name]=response['node']
    graph={'version':'provider-call-graph-v1','calls':{'application_version':json.loads(plans['primary'].source_call_json)['parameters']['extra']['agentc_route_context']['application_version']['sha256'],
        'callback_version':'physical-role-sdk-v1','item_count':1,'max_workers':1,'branches':[
            {'position':nodes[name]['position'],'call_site_version':nodes[name]['key']['call_site_version'],'plan':nodes[name]['plan']} for name in ['primary','consumer']]},
        'dependencies':[{'producer':{'item':0,'call':0},'consumer':{'item':0,'call':1}}],
        'results':{'kind':'callback_items','items':[0]}}
    dispatches=[];bindings=[]
    for ordinal,name in enumerate(['primary','fallback','validation','shadow','exploration','consumer']):
        slot={'position':nodes[name]['position'],'ordinal':0 if name=='consumer' else ordinal}
        dispatches.append({'slot':slot,'role':'primary' if name=='consumer' else name,'call_site_version':nodes[name]['key']['call_site_version'],
            'plan':nodes[name]['plan'],'activation':{'kind':'on_failure','predecessor':{'position':nodes['primary']['position'],'ordinal':0}} if name=='fallback' else {'kind':'always'}})
        bindings.append({'slot':slot,'runtime':nodes[name]['runtime'],'request_sha256':_request_fingerprint(bodies[name],protocol),
            'transport_sha256':_transport_fingerprint(client.chat.completions,protocol)})
    prepared={'program':{'version':'provider-role-graph-v1','graph':graph,'dispatches':dispatches},'bindings':bindings,
        'logical_requests':[{'position':nodes[name]['position'],'sha256':nodes[name]['logical_request_sha256']} for name in ['primary','consumer']]}
    input_hash=comparison_fingerprint([0],version=GRAPH_COMPARISON_VERSION)
    def batch():
        b=ProviderReceiptBatch(graph['calls']['application_version'],'physical-role-sdk-v1',1)
        b.prepare_role_graph(prepared,input_hash)
        return b
    def invoke(b,name,*,body=None,physical=None,kind=None):
        owner=plans['consumer' if name=='consumer' else 'primary']
        with b.item(0):
            note_logical_call(1 if name=='consumer' else 0);bind_plan(owner)
            provider_call=instrument_provider(client.chat.completions.create.__wrapped__,client.chat.completions,protocol)
            with provider_dispatch(kind or ('primary' if name=='consumer' else name),plan=owner,primary_plan=owner,
                dispatch_plan=physical or plans[name]):
                return provider_call(**(body or bodies[name]))

    import copy
    prepared['source_contracts']=[{'position':nodes[name]['position'],'sha256':nodes[name]['source_contract_sha256']} for name in ['primary','consumer']]
    reference=copy.deepcopy(prepared)
    reference['program']['dispatches']=[d for d in reference['program']['dispatches'] if d['role']=='primary']
    reference['bindings']=[d for d in reference['bindings'] if d['slot']['ordinal']==0]
    def prepare(program,pair=None):
        b=ProviderReceiptBatch(graph['calls']['application_version'],'physical-role-sdk-v1',1)
        b.prepare_role_graph(program,input_hash,source_pair_id=pair)
        return b
    def execute(b,*,is_reference=False,failed=False,bad=False):
        global failing,adverse
        failing=failed;adverse=False;start=time.perf_counter()
        if failed:
            try:invoke(b,'primary')
            except openai.InternalServerError:pass
            else:raise AssertionError('fixture primary must fail')
            invoke(b,'fallback')
        else:invoke(b,'primary')
        if not is_reference:
            for name in ['validation','shadow','exploration']:invoke(b,name)
        adverse=bad;result=invoke(b,'consumer');adverse=False
        b.seal([2]);b.finish_economics((time.perf_counter()-start)*1000)
        comparison={'version':GRAPH_COMPARISON_VERSION,'input_sha256':input_hash,'output_sha256':comparison_fingerprint([result.choices[0].message.content],version=GRAPH_COMPARISON_VERSION)}
        value=json.loads(agentc._native.optimize_record_provider_receipt(json.dumps({'operation':'finish_role_graph','lease_id':b.graph_preparation_id,'comparison':comparison})))
        assert value['status']=='recorded',value
        return value['execution']
    def compare(r,c):
        value=json.loads(agentc._native.optimize_compare_provider_role_graph(r['execution_id'],c['execution_id']))
        assert value['status'] in {'recorded','already_recorded'},value
        return value
    r=execute(prepare(reference),is_reference=True)
    c=execute(prepare(prepared),failed=True)
    source=compare(r,c)
    assert source['quality']['lifetime_pairs']==1 and source['acquisition_cost_usd'] is None
    source_id=source['pair_id']
    rb=prepare(reference,source_id);cb=prepare(prepared,source_id)
    r=execute(rb,is_reference=True);c=execute(cb)
    paired=compare(r,c)
    assert len(paired['quality']['prepared_pairs'])==1
    old=prepare(prepared,source_id)
    bad_pairs=[]
    for _ in range(2):
        rb=prepare(reference,source_id);cb=prepare(prepared,source_id)
        bad_pairs.append(compare(execute(rb,is_reference=True),execute(cb,bad=True)))
    assert bad_pairs[-1]['quality']['disabled'] is not None
    before=len(calls)
    try:invoke(old,'primary')
    except RuntimeError:pass
    else:raise AssertionError('pre-disable role graph dispatched')
    assert len(calls)==before==29
    retried=compare(r,c)
    assert retried['status']=='already_recorded' and retried['pair_id']==paired['pair_id']
    assert retried['quality']['lifetime_pairs']==4 and retried['quality']['disabled'] is not None
    assert not retried['quality']['prepared_pairs']
    financial=None
    if financial_mode:
        def cost_command(operation,**data):
            value=json.loads(agentc._native.optimize_record_provider_receipt(json.dumps({'operation':operation,**data})))
            assert value['status']=='recorded' and value['admission']=='financial_projection_only',value
            return value
        before_billing=None
        if billing_mode:
            from agentc._provider_receipts import _provider_identity_fingerprint
            before_billing=cost_command('graph_execution_costs',kind='roles',execution_id=paired['candidate_execution_id'])['projection']
            assert before_billing['costs']['billed_usd'] is None
            bills=agentc.provider_billing_receipts(c['batch_id'])
            assert len(bills)==5
            for receipt in bills:
                generation=next(g for g in generations if _provider_identity_fingerprint(g)==receipt.generation_sha256)
                settled=agentc.reconcile_openrouter_generation(receipt,generation_id=generation,client=client)
                assert settled.status=='recorded',settled
                again=agentc.reconcile_openrouter_generation(receipt,generation_id=generation,client=client)
                assert again.status=='already_recorded',again
            assert len(metadata)==5 and len(calls)==29
        good=cost_command('graph_execution_costs',kind='roles',execution_id=paired['candidate_execution_id'])['projection']
        failed=cost_command('graph_execution_costs',kind='roles',execution_id=source['candidate_execution_id'])['projection']
        window=cost_command('graph_cost_window',kind='roles',key=c['key'],runtime=c['runtime'],window_size=paired['quality']['window_size'])['window']
        lifetime=cost_command('graph_acquisition_costs',driver_id=c['acquisition']['driver_id'])['acquisition']
        assert good['costs']['physical_invocations']==5
        if billing_mode:
            assert good['costs']['billed_usd']['lower_usd']<=.1<=good['costs']['billed_usd']['upper_usd']
            assert good['costs']['published_token_tariff'] is None
            assert before_billing['source_sequence']==good['source_sequence'] and before_billing['journal_sha256']==good['journal_sha256']
        else:
            assert good['costs']['billed_usd'] is None
            assert good['costs']['published_token_tariff']['interval']=={'lower_nanodollars':46800,'upper_nanodollars':46800}
        assert failed['costs']['physical_invocations']==6
        assert failed['costs']['unpriced_usd_invocations']==6
        assert failed['costs']['unpriced_tariff_invocations']==(6 if billing_mode else 1)
        assert failed['costs']['billed_usd'] is None and failed['costs']['published_token_tariff'] is None
        assert window['lifetime_executions']==4 and len(window['observations'])==4
        assert window['billed_usd_mean'] is None and window['published_token_tariff_mean'] is None
        assert lifetime['costs']['physical_invocations']==29 and lifetime['economics']['unfinished_batches']==1
        assert lifetime['costs']['billed_usd'] is None and lifetime['costs']['published_token_tariff'] is None
        selection={'objective':'cost','min_paired_observations':20,'now_us':int(time.time()*1e6),'max_profile_age_us':24*60*60*1000000,'divergence_exposure_budget':paired['quality']['policy']['exposure_budget'],'max_expected_cost_usd':None,'max_expected_latency_ms':None}
        money={'quantity':'billed_usd'} if billing_mode else {'quantity':'published_token_tariff_usd','snapshot_sha256':good['costs']['published_token_tariff']['snapshot_sha256'],'max_expected_nanodollars':None}
        request={'kind':'roles','source_pair_ids':[paired['pair_id']],'window_size':paired['quality']['window_size'],'remaining_batches':1000,
            'policy':{'selection':selection,'quality':paired['quality']['policy'],'money':money,'max_workers':1,'max_physical_invocations':6}}
        admission=json.loads(agentc._native.optimize_record_provider_receipt(json.dumps({'operation':'graph_admission','request':request})))
        assert admission['status']=='recorded' and admission['admission']=='evidence_selection_only',admission
        assert admission['decision']['selected_reference'] and admission['decision']['alternatives'][0]['rejection']=='insufficient_complete_graph_evidence'
        assert len(calls)==29 and len(metadata)==(5 if billing_mode else 0)
        financial={'good':good,'failed':failed,'window':window,'lifetime':lifetime,'before_billing':before_billing,'mode':'billing' if billing_mode else 'tariff','metadata_gets':len(metadata),'admission':admission}
        assert 'PRIVATE_' not in json.dumps(financial)
    print(json.dumps({'sdk_sends':len(calls),'source':source,'paired':paired,'bad_pairs':bad_pairs,'retry':retried,'held_batch_id':old.batch_id,'financial':financial}))
finally:
    if tariff_scope:tariff_scope.__exit__(None,None,None)
    agentc.shutdown();client.close()
"""

def test_role_graph_sdk_pairs_complete_results_and_holds_pre_disable_acquisition(tmp_path):
    env = {
        **os.environ, "AGENTC_STORAGE_PATH": str(tmp_path),
        "AGENTC_OPTIMIZE": "1", "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": "1000",
        "AGENTC_OPTIMIZE_EXPLORATION": "0", "AGENTC_OPTIMIZE_SHADOW": "0",
        "AGENTC_CAPTURE_CONTENT": "0", "AGENTC_CAPTURE_EMBEDDINGS": "0",
    }
    result = subprocess.run(
        [sys.executable, "-I", "-c", GRAPH_ROLE_QUALITY_WORKER], env=env,
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    proof=json.loads(result.stdout)
    assert proof["sdk_sends"]==29
    (tmp_path/"provider-graph-role-quality-sdk-proof.json").write_text(result.stdout)
    restart=subprocess.run(
        [sys.executable,"-I","-c",r"""
import agentc,json,sys
agentc.init()
try:
    proof=json.loads(sys.argv[1]);p=proof['paired']
    result=json.loads(agentc._native.optimize_compare_provider_role_graph(p['reference_execution_id'],p['candidate_execution_id']))
    assert result['status']=='already_recorded' and result['pair_id']==p['pair_id']
    assert result['quality']['lifetime_pairs']==4 and result['quality']['disabled'] is not None
    assert not result['quality']['prepared_pairs']
finally:agentc.shutdown()
""",result.stdout],env=env,capture_output=True,text=True,timeout=60,
    )
    assert restart.returncode==0,restart.stderr
    sql=r"""
import json,sqlite3,hashlib,sys
from pathlib import Path
assert not any(n=='agentc' or n.startswith('agentc.') for n in sys.modules)
conn=sqlite3.connect(Path(sys.argv[1])/'cost_model.db')
assert conn.execute('SELECT COUNT(*) FROM provider_graph_role_execution').fetchone()[0]==8
assert conn.execute('SELECT COUNT(*) FROM provider_graph_role_pair').fetchone()[0]==4
assert conn.execute('SELECT COUNT(*) FROM provider_graph_role_quality_event').fetchone()[0]==4
assert conn.execute('SELECT COUNT(*) FROM provider_receipt_invocation').fetchone()[0]==29
for table in ['provider_graph_pair','provider_graph_quality_event']:
    if conn.execute("SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name=?)",(table,)).fetchone()[0]:
        assert conn.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]==0
contexts=[]
for raw in conn.execute('SELECT lease_json FROM provider_graph_preparation'):
    lease=json.loads(raw[0]);ctx=lease.get('role_comparison_context')
    if ctx:
        assert 'PRIVATE_' not in raw[0]
        contexts.append(ctx)
assert len(contexts)==7
assert contexts[0]['generation']==contexts[1]['generation']
assert conn.execute('SELECT COUNT(*) FROM provider_graph_role_quality_state WHERE disabled_at IS NOT NULL').fetchone()[0]==1
assert not conn.execute('PRAGMA foreign_key_check').fetchall()
"""
    audit=subprocess.run([sys.executable,"-I","-c",sql,str(tmp_path)],capture_output=True,text=True,timeout=30)
    assert audit.returncode==0,audit.stderr


@pytest.mark.parametrize("mode", ["tariff", "billing"])
def test_role_graph_financial_sdk_views_preserve_unknown_liabilities_and_restart(tmp_path, mode):
    env = {
        **os.environ, "AGENTC_STORAGE_PATH": str(tmp_path),
        "AGENTC_OPTIMIZE": "1", "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": "1000",
        "AGENTC_OPTIMIZE_EXPLORATION": "0", "AGENTC_OPTIMIZE_SHADOW": "0",
        "AGENTC_CAPTURE_CONTENT": "0", "AGENTC_CAPTURE_EMBEDDINGS": "0",
        "AGENTC_GRAPH_FINANCIAL_TEST": "2" if mode == "billing" else "1",
    }
    result = subprocess.run(
        [sys.executable, "-I", "-c", GRAPH_ROLE_QUALITY_WORKER], env=env,
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    proof = json.loads(result.stdout)
    assert proof["sdk_sends"] == 29 and proof["financial"] is not None
    path = tmp_path / "provider-graph-financial-sdk-proof.json"
    path.write_text(result.stdout)
    restarted = subprocess.run(
        [sys.executable, "-I", "-c", r"""
import agentc,json,sys
from pathlib import Path
proof=json.loads(Path(sys.argv[1]).read_text());f=proof['financial']
agentc.init()
try:
    def cost(operation,**data):
        result=json.loads(agentc._native.optimize_record_provider_receipt(json.dumps({'operation':operation,**data})))
        assert result['status']=='recorded' and result['admission']=='financial_projection_only',result
        return result
    for label in ['good','failed']:
        original=f[label]
        assert cost('graph_execution_costs',kind='roles',execution_id=original['execution_id'])['projection']==original
    w=f['window']
    assert cost('graph_cost_window',kind='roles',key=w['key'],runtime=w['runtime'],window_size=proof['paired']['quality']['window_size'])['window']==w
    assert cost('graph_acquisition_costs',driver_id=f['lifetime']['economics']['driver_id'])['acquisition']==f['lifetime']
    original=f['admission']
    admitted=json.loads(agentc._native.optimize_record_provider_receipt(json.dumps({'operation':'graph_admission','request':original['decision']['request']})))
    assert admitted==original
finally:agentc.shutdown()
""", str(path)], env=env, capture_output=True, text=True, timeout=60,
    )
    assert restarted.returncode == 0, restarted.stderr
    sql = r"""
import json,sqlite3,sys,hashlib
from pathlib import Path
assert not any(n=='agentc' or n.startswith('agentc.') for n in sys.modules)
path=Path(sys.argv[1]);proof=json.loads(path.read_text());f=proof['financial']
conn=sqlite3.connect('file:'+str(path.parent/'cost_model.db')+'?mode=ro',uri=True);conn.execute('BEGIN')
canon=lambda v:json.dumps(v,sort_keys=True,separators=(',',':'),ensure_ascii=False)
assert conn.execute('SELECT COUNT(*) FROM provider_graph_role_execution').fetchone()[0]==8
assert conn.execute('SELECT COUNT(*) FROM provider_graph_role_pair').fetchone()[0]==4
assert conn.execute('SELECT COUNT(*) FROM provider_graph_role_quality_event').fetchone()[0]==4
assert conn.execute('SELECT COUNT(*) FROM provider_receipt_invocation').fetchone()[0]==29
assert conn.execute('SELECT COUNT(*) FROM provider_billing_settlement').fetchone()[0]==(5 if f['mode']=='billing' else 0)
assert conn.execute('SELECT COUNT(*) FROM provider_billing_attempt').fetchone()[0]==(5 if f['mode']=='billing' else 0)
assert conn.execute('SELECT SUM(lifetime_executions) FROM provider_graph_role_execution_state').fetchone()[0]==8
for o in [f['good'],f['failed'],*f['window']['observations']]:
    row=conn.execute('SELECT rowid,execution_json FROM provider_graph_role_execution WHERE execution_id=?',(o['execution_id'],)).fetchone()
    e=json.loads(row[1]);assert o['source_sequence']==row[0] and o['journal_sha256']==e['journal_sha256']
    original=conn.execute('SELECT invocation_id,invocation_json,completion_json FROM provider_receipt_invocation WHERE batch_id=? ORDER BY item,logical_call,ordinal',(e['batch_id'],)).fetchall()
    assert len(original)==o['costs']['physical_invocations']==len(o['costs']['receipts'])
    for r,(id,raw,done) in zip(o['costs']['receipts'],original):
        digest=hashlib.sha256(canon({'version':'provider-billing-binding-v1','batch_id':e['batch_id'],'invocation_id':id,'invocation':raw,'completion':done}).encode()).hexdigest()
        assert r['invocation_id']==id and r['receipt_sha256']==digest
        invocation=json.loads(raw);c=json.loads(done)
        if f['mode']=='billing':
            assert r['tariff'] is None and c['cost_source']!='provider_reported'
            settlement=conn.execute('SELECT invoice_json FROM provider_billing_settlement WHERE invocation_id=?',(id,)).fetchone()
            if settlement:
                invoice=json.loads(settlement[0]);assert invoice['receipt_sha256']==digest and r['billed_usd']==invoice['cost_usd']==.02
                assert r['settlement_sha256']==hashlib.sha256(settlement[0].encode()).hexdigest()
                reserved,finished=conn.execute('SELECT result_input_json,result_json FROM provider_billing_attempt WHERE invocation_id=?',(id,)).fetchone()
                assert reserved==finished and json.loads(reserved)['invoice']==invoice
            else:assert r['billed_usd'] is None and r['settlement_sha256'] is None
            assert e['acquisition']['cost_complete'] is False
        elif c['status']=='completed':
            input_rate,output_rate=(150,600) if c['model_version'].startswith('gpt-4o-mini') else (2500,10000)
            expected=c['input_tokens']*input_rate+c['output_tokens']*output_rate
            assert r['billed_usd'] is None and c['cost_source']!='provider_reported'
            assert r['tariff']['interval']=={'lower_nanodollars':expected,'upper_nanodollars':expected}
            assert r['tariff']['snapshot_sha256']==invocation['tariff_declaration']['snapshot_sha256']
            assert r['settlement_sha256'] is None
        else:
            assert r['billed_usd'] is None and r['tariff'] is None
assert f['lifetime']['economics']['unfinished_batches']==1
known=.1 if f['mode']=='billing' else 0.
assert f['lifetime']['costs']['known_billed_usd']['lower_usd']<=known<=f['lifetime']['costs']['known_billed_usd']['upper_usd']
assert f['metadata_gets']==(5 if f['mode']=='billing' else 0)
decision=f['admission']['decision'];digest=decision.pop('evidence_sha256');decision['evidence_sha256']=''
evidence=f['admission']['canonical_evidence_json']
assert json.loads(evidence)==decision
assert hashlib.sha256(evidence.encode()).hexdigest()==digest
assert decision['selected_reference'] and decision['selected_source_pair_id'] is None
assert not conn.execute('PRAGMA foreign_key_check').fetchall()
"""
    audited = subprocess.run(
        [sys.executable, "-I", "-c", sql, str(path)], capture_output=True,
        text=True, timeout=30,
    )
    assert audited.returncode == 0, audited.stderr


REFRESH_SAMPLING_SDK = DISCOVERY_SDK.split("    with agentc.application_version('discovery-sdk-v1')")[0].replace(
    "rate=float(os.environ['AGENTC_GRAPH_DISCOVERY_RATE']);sends=[];callbacks=[]",
    "rate=1.;sends=[];callbacks=[];steps=2;fault=os.environ['AGENTC_GRAPH_REFRESH_FAULT']=='1'",
).replace("range(2)", "range(steps)").replace(
    "    usage={'prompt_tokens':2",
    "    if fault and len(sends)==14:return httpx.Response(500,json={'error':{'message':'fixture','type':'fixture'}})\n    usage={'prompt_tokens':2",
) + r"""
    money={'quantity':quantity}
    if quantity=='billed_usd':money.update(workflow_usd=.08,lifetime_usd=1.)
    else:money.update(snapshot_sha256=json.loads(agentc._native.optimize_record_provider_receipt(json.dumps({'operation':'tariff_policy'})))['snapshot_sha256'],workflow_nanodollars=80000,lifetime_nanodollars=1000000)
    with agentc.application_version('refresh-sdk-v1'),agentc.optimization_scope('refresh-sdk',optimize=True):
        learning={'policy':{'sampling_rate':1.,'max_workflows':20,'max_samples':20,'max_physical_invocations':6,
            'max_lifetime_physical_invocations':30,'refresh_interval_workflows':2,'replay_contract_sha256':'d'*64,
            'commitments':{'money':money,'max_open_workflows':1,'max_pending_invocations':1}}}
        observed=[]
        for n in range(5):
            steps=2 if n<2 else 3
            result=agentc.parallel_map(task,['INPUT_'+str(n)],comparison='graph',graph_sampling=learning,on_observation=observed.append)
            assert result==['PRIVATE_DISCOVERY_RESULT'],result
            report=observed[-1].graph_sampling
            assert report['ticket']['ordinal']==n+1 and report['ticket']['sampled'],report
            if n==2 and fault:
                assert report['completion']['status']=='incomplete' and report['candidate_error_type']=='InternalServerError',report
                assert report['commitments']['held_workflows']==1 and report['commitments']['unknown_cost_workflows']==1,report
            else:assert report['completion']['status']=='paired',report
        reports=[o.graph_sampling for o in observed];first=reports[0]['completion']['pair_id']
        assert reports[1]['ticket']['discovered_pair_id']==first
        assert reports[2]['ticket']['refresh']['source_pair_id']==first and reports[2]['ticket']['refresh']['reason']=='interval'
        assert 'discovered_pair_id' not in reports[2]['ticket']
        if fault:
            assert reports[3]['ticket']['refresh']['source_pair_id']==first
            assert reports[4]['ticket']['discovered_pair_id']==reports[3]['completion']['pair_id']
        else:
            assert reports[3]['ticket']['discovered_pair_id']==reports[2]['completion']['pair_id']
            assert reports[4]['ticket']['refresh']['source_pair_id']==reports[2]['completion']['pair_id']
        assert len({r['ticket']['driver_id'] for r in reports})==1
        assert len(sends)==26 and len(callbacks)==10
        assert reports[-1]['acquisition']['costs']['physical_invocations']==26
        assert reports[-1]['commitments']['held_workflows']==int(fault)
        before=(len(sends),len(callbacks))
        try:agentc.parallel_map(task,['EXHAUSTED'],comparison='graph',graph_sampling=learning)
        except RuntimeError as exc:assert 'lifetime physical' in str(exc),exc
        else:raise AssertionError('refresh reset original physical allowance')
        assert before==(len(sends),len(callbacks))
        changed=json.loads(json.dumps(learning));changed['policy']['refresh_interval_workflows']=1
        try:agentc.parallel_map(task,['CHANGED_POLICY'],comparison='graph',graph_sampling=changed)
        except RuntimeError:pass
        else:raise AssertionError('refresh policy changed original driver')
        assert before==(len(sends),len(callbacks))
        proof={'mode':mode,'quantity':quantity,'fault':fault,'sdk_sends':len(sends),'callback_calls':len(callbacks),
            'reports':reports,'last_ticket_id':reports[-1]['ticket']['ticket_id']}
        assert 'PRIVATE_' not in json.dumps(proof);print(json.dumps(proof))
finally:
    if scope:scope.__exit__(None,None,None)
    agentc.shutdown()
    if mode=='async':asyncio.run(client.close())
    else:client.close()
"""


@pytest.mark.parametrize("quantity", ["billed_usd", "published_token_tariff_usd"])
@pytest.mark.parametrize("fault", [False, True])
@pytest.mark.parametrize("mode", ["sync", "async"])
def test_graph_sampling_sdk_refreshes_changed_topology_without_resetting_costs(tmp_path, quantity, fault, mode):
    env = {**os.environ, "AGENTC_STORAGE_PATH": str(tmp_path), "AGENTC_OPTIMIZE": "1",
           "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": "1000", "AGENTC_OPTIMIZE_EXPLORATION": "1",
           "AGENTC_OPTIMIZE_SHADOW": "1", "AGENTC_CAPTURE_CONTENT": "0", "AGENTC_CAPTURE_EMBEDDINGS": "0",
           "AGENTC_GRAPH_DISCOVERY_QUANTITY": quantity, "AGENTC_GRAPH_DISCOVERY_MODE": mode,
           "AGENTC_GRAPH_REFRESH_FAULT": str(int(fault))}
    env.pop("AGENTC_EVAL_PLANNER_MODE", None)
    result = subprocess.run([sys.executable, "-I", "-c", REFRESH_SAMPLING_SDK], env=env, capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stderr
    proof_path = tmp_path / "provider-graph-refresh-sdk-proof.json"
    proof_path.write_text(result.stdout)
    restart = subprocess.run([sys.executable, "-I", "-c", r"""
import agentc,json,sys,uuid
from pathlib import Path
p=json.loads(Path(sys.argv[1]).read_text());agentc.init()
try:
 for report in p['reports']:
    r=json.loads(agentc._native.optimize_provider_graph_sampling(json.dumps({'operation':'finish','ticket_id':report['ticket']['ticket_id']})))
    assert r['status']=='recorded' and r['sampling']==report['completion'],r
 r=json.loads(agentc._native.optimize_provider_graph_sampling(json.dumps({'operation':'commitments','ticket_id':p['last_ticket_id']})))
 assert r['status']=='recorded' and r['sampling']['commitments']==p['reports'][-1]['commitments'],r
 driver=p['reports'][-1]['acquisition']['economics']['driver_id']
 r=json.loads(agentc._native.optimize_record_provider_receipt(json.dumps({'operation':'graph_acquisition_costs','driver_id':driver})))
 assert r['status']=='recorded' and r['acquisition']==p['reports'][-1]['acquisition'],r
 request=p['reports'][-1]['ticket']['request'];request['reference_batch']['batch_id']=uuid.uuid4().hex;request['candidate_batch']['batch_id']=uuid.uuid4().hex
 r=json.loads(agentc._native.optimize_provider_graph_sampling(json.dumps({'operation':'begin','request':request})))
 assert r['status']=='abstained' and 'lifetime physical' in r['reason'],r
finally:agentc.shutdown()
""", str(proof_path)], env=env, capture_output=True, text=True, timeout=60)
    assert restart.returncode == 0, restart.stderr
    audit = subprocess.run([sys.executable, "-I", "-c", r"""
import sqlite3,json,hashlib,sys
from pathlib import Path
assert not any(n=='agentc' or n.startswith('agentc.') for n in sys.modules)
p=Path(sys.argv[1]);proof=json.loads(p.read_text());c=sqlite3.connect(p.parent/'cost_model.db');fault=proof['fault']
assert c.execute('SELECT workflows,samples FROM provider_graph_sampling_state').fetchone()==(5,5)
assert c.execute('SELECT COUNT(*) FROM provider_receipt_invocation').fetchone()[0]==proof['sdk_sends']==26
assert c.execute('SELECT COUNT(*) FROM provider_graph_sampling_invocation').fetchone()[0]==26
assert c.execute('SELECT COUNT(*) FROM provider_graph_pair').fetchone()[0]==5-int(fault)
rows=c.execute('SELECT generation,checkpoint_sha256,checkpoint_json FROM provider_graph_sampling_template ORDER BY generation').fetchall()
assert len(rows)==3-int(fault)
prior=None
for n,h,raw in rows:
 assert hashlib.sha256(raw.encode()).hexdigest()==h
 value=json.loads(raw);assert value['generation']==n and value['prior_pair_id']==prior;prior=value['pair_id']
 assert c.execute('SELECT COUNT(*) FROM provider_graph_pair WHERE pair_id=?',(prior,)).fetchone()[0]==1
assert c.execute('SELECT pair_id FROM provider_graph_sampling_discovered').fetchone()[0]==prior
pair=json.loads(c.execute('SELECT pair_json FROM provider_graph_pair WHERE pair_id=?',(prior,)).fetchone()[0])
execution=json.loads(c.execute('SELECT execution_json FROM provider_graph_execution WHERE execution_id=?',(pair['candidate_execution_id'],)).fetchone()[0])
assert len(execution['spec']['calls']['branches'])==3
assert c.execute('SELECT COUNT(*) FROM provider_receipt_invocation WHERE json_extract(completion_json,"$.status")="failed"').fetchone()[0]==int(fault)
assert not c.execute('PRAGMA foreign_key_check').fetchall()
""", str(proof_path)], capture_output=True, text=True, timeout=30)
    assert audit.returncode == 0, audit.stderr


CAMPAIGN_SAMPLING_SDK = DEPLOYMENT_SAMPLING_SDK.split("        for n in range(4):")[0].replace(
    "sends=[];callbacks=[];strong=", "fault=os.environ['AGENTC_GRAPH_CAMPAIGN_FAULT']=='1';sends=[];callbacks=[];strong=",
).replace(
    "    content='PRIVATE_DEPLOYED_RESULT'",
    "    if fault and len(sends)==82:return httpx.Response(500,json={'error':{'message':'fixture','type':'fixture'}})\n    content='PRIVATE_DEPLOYED_RESULT'",
).replace(
    "        observed=[]\n",
    "        campaign_money={'quantity':quantity}\n"
    "        if quantity=='billed_usd':campaign_money.update(workflow_usd=.05,lifetime_usd=5.)\n"
    "        else:campaign_money.update(snapshot_sha256=money['snapshot_sha256'],workflow_nanodollars=50000,lifetime_nanodollars=5000000)\n"
    "        campaign={'campaign_id':'b'*64,'commitments':{'money':campaign_money,'max_open_workflows':1,'max_pending_invocations':1},'max_workflows':1000,'max_physical_invocations':4,'max_lifetime_physical_invocations':1000}\n"
    "        learning['policy']['campaign']=campaign\n"
    "        observed=[]\n",
) + r"""
        from agentc._provider_receipts import ProviderReceiptBatch
        source_report=observed[-1].graph_sampling
        template=source_report['candidate_execution_id'];pair_id=source_report['completion']['pair_id']
        independent={'kind':'primary','source_pair_ids':[pair_id],'window_size':source_report['ticket']['window_size'],**admission,'campaign':campaign}
        before_report=ProviderReceiptBatch.graph_campaign_report(campaign['campaign_id'])
        assert before_report['workflows']==20 and before_report['physical_invocations']==80 and before_report['held_workflows']==0,before_report
        if fault:
            try:agentc.parallel_map(task,['FAILED_INDEPENDENT'],comparison='graph',graph_template_id=template,graph_admission=independent,on_observation=observed.append)
            except openai.InternalServerError:pass
            else:raise AssertionError('independent visible failure was swallowed')
            assert len(sends)==82 and len(callbacks)==41
        else:
            for n in range(3):
                if n==1:
                    result=agentc.parallel_map(task,['SAMPLED_DEPLOY'],comparison='graph',graph_sampling=learning,on_observation=observed.append)
                    assert observed[-1].graph_sampling['ticket']['deployed'] and observed[-1].graph_sampling['campaign']['workflows']==22,observed[-1]
                else:
                    result=agentc.parallel_map(task,['INDEPENDENT_'+str(n)],comparison='graph',graph_template_id=template,graph_admission=independent,on_observation=observed.append)
                    assert observed[-1].graph_decision and not observed[-1].graph_decision['selected_reference'],observed[-1]
                assert result==['PRIVATE_DEPLOYED_RESULT'],result
            assert len(sends)==86 and len(callbacks)==43
        import uuid
        pending=json.loads(json.dumps(source_report['ticket']['request']))
        pending['reference_batch']['batch_id']=uuid.uuid4().hex;pending['candidate_batch']['batch_id']=uuid.uuid4().hex
        pending['reference_batch']['callback_version']='pending-independent-driver';pending['candidate_batch']['callback_version']='pending-independent-driver'
        pending['policy']['max_samples']=0
        opened=json.loads(agentc._native.optimize_provider_graph_sampling(json.dumps({'operation':'begin','request':pending})))
        assert opened['status']=='recorded',opened
        final=ProviderReceiptBatch.graph_campaign_report(campaign['campaign_id'])
        assert final['workflows']==(22 if fault else 24) and final['physical_invocations']==len(sends),final
        assert final['held_workflows']==1+int(fault) and final['unknown_cost_workflows']==int(fault),final
        assert final['open_workflows']==1 and final['pending_invocations']==0 and not final['budget_exceeded'],final
        if quantity=='billed_usd':
            known=sum(.01 if call['model']==strong else .001 for call in sends[:80])
            expected=(known+.05 if fault else sum(.01 if call['model']==strong else .001 for call in sends))+.05
            assert expected<=final['billed_committed_upper_usd']<expected+1e-12,final
        else:
            expected=sum(15000 if call['model']==strong else 900 for call in sends[:80])+(50000 if fault else sum(15000 if call['model']==strong else 900 for call in sends[80:]))
            assert final['tariff_committed_upper_nanodollars']==expected+50000,final
        def second_driver(item):return task(item)
        before=(len(sends),len(callbacks))
        for kind in ['independent','sampling','different_driver']:
            try:
                if kind=='independent':agentc.parallel_map(task,['EXHAUSTED'],comparison='graph',graph_template_id=template,graph_admission=independent)
                else:agentc.parallel_map(second_driver if kind=='different_driver' else task,['EXHAUSTED'],comparison='graph',graph_sampling=learning)
            except RuntimeError:pass
            else:raise AssertionError('shared campaign allowed another callback')
            assert before==(len(sends),len(callbacks))
        changed=json.loads(json.dumps(independent));changed['campaign']['max_workflows']+=1
        try:agentc.parallel_map(task,['CHANGED'],comparison='graph',graph_template_id=template,graph_admission=changed)
        except RuntimeError:pass
        else:raise AssertionError('changed independent campaign replenished allowance')
        assert before==(len(sends),len(callbacks))
        for replacement in [None, {**campaign,'campaign_id':'d'*64}]:
            escaped=json.loads(json.dumps(independent))
            if replacement is None:escaped.pop('campaign')
            else:escaped['campaign']=replacement
            try:agentc.parallel_map(task,['OMITTED_CAMPAIGN'],comparison='graph',graph_template_id=template,graph_admission=escaped)
            except RuntimeError as exc:assert 'original campaign commitment' in str(exc),exc
            else:raise AssertionError('enrolled driver escaped its campaign commitment')
            assert before==(len(sends),len(callbacks))
        for kwargs in [{'comparison':'graph','graph_template_id':template},{'comparison':'graph'},{'comparison':False}]:
            try:agentc.parallel_map(task,['UNBUDGETED_ACQUISITION'],**kwargs)
            except RuntimeError as exc:assert 'original campaign commitment' in str(exc),exc
            else:raise AssertionError('unbudgeted graph acquisition escaped its campaign commitment')
            assert before==(len(sends),len(callbacks))
        from agentc._provider_receipts import GraphCampaignBindingStop
        metadata=source_report['ticket']['request']['reference_batch']
        direct=ProviderReceiptBatch(metadata['application_version'],metadata['callback_version'],metadata['item_count'])
        for streaming in [False,True]:
            with direct.item(0):
                try:
                    call=client.chat.completions.create(model=strong,messages=[{'role':'user','content':'PRIVATE_UNPREPARED_BYPASS'}],max_tokens=128,stream=streaming)
                    if mode=='async':asyncio.run(call)
                except GraphCampaignBindingStop:pass
                else:raise AssertionError('unprepared SDK dispatch escaped its campaign binding')
        assert before==(len(sends),len(callbacks))
        assert ProviderReceiptBatch.graph_campaign_report(campaign['campaign_id'])==final
        proof={'mode':mode,'quantity':quantity,'fault':fault,'sdk_sends':len(sends),'callbacks':len(callbacks),
            'campaign':campaign,'before':before_report,'final':final,'template_id':template,'independent':independent,
            'sampling_request':source_report['ticket']['request'],'learning_reports':[o.graph_sampling for o in observed if o.graph_sampling]}
        assert 'PRIVATE_' not in json.dumps(proof);print(json.dumps(proof))
finally:
    if scope:scope.__exit__(None,None,None)
    agentc.shutdown()
    if mode=='async':asyncio.run(client.close())
    else:client.close()
"""


@pytest.mark.parametrize("quantity", ["billed_usd", "published_token_tariff_usd"])
@pytest.mark.parametrize("fault", [False, True])
@pytest.mark.parametrize("mode", ["sync", "async"])
def test_shared_graph_campaign_sdk_charges_sampling_and_independent_deployments(tmp_path, quantity, fault, mode):
    env = {**os.environ, "AGENTC_STORAGE_PATH": str(tmp_path), "AGENTC_OPTIMIZE": "1",
           "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": "1000", "AGENTC_OPTIMIZE_EXPLORATION": "1",
           "AGENTC_OPTIMIZE_SHADOW": "1", "AGENTC_CAPTURE_CONTENT": "0", "AGENTC_CAPTURE_EMBEDDINGS": "0",
           "AGENTC_GRAPH_DEPLOYMENT_QUANTITY": quantity, "AGENTC_GRAPH_DEPLOYMENT_MODE": mode,
           "AGENTC_GRAPH_DEPLOYMENT_HELD": "0", "AGENTC_GRAPH_CAMPAIGN_FAULT": str(int(fault))}
    env.pop("AGENTC_EVAL_PLANNER_MODE", None)
    result = subprocess.run([sys.executable, "-I", "-c", CAMPAIGN_SAMPLING_SDK], env=env, capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stderr
    proof_path = tmp_path / "provider-graph-campaign-sdk-proof.json"
    proof_path.write_text(result.stdout)
    restart = subprocess.run([sys.executable, "-I", "-c", r"""
import agentc,json,sys,uuid
from pathlib import Path
from agentc._provider_receipts import ProviderReceiptBatch
p=json.loads(Path(sys.argv[1]).read_text());agentc.init()
try:
 assert ProviderReceiptBatch.graph_campaign_report(p['campaign']['campaign_id'])==p['final']
 for report in p['learning_reports']:
  value=json.loads(agentc._native.optimize_provider_graph_sampling(json.dumps({'operation':'finish','ticket_id':report['ticket']['ticket_id']})))
  assert value['status']=='recorded' and value['sampling']==report['completion'],value
 request=p['sampling_request'];request['reference_batch']['batch_id']=uuid.uuid4().hex;request['candidate_batch']['batch_id']=uuid.uuid4().hex
 value=json.loads(agentc._native.optimize_provider_graph_sampling(json.dumps({'operation':'begin','request':request})))
 assert value['status']=='abstained' and 'campaign' in value['reason'],value
 for replacement in [None,{**p['campaign'],'campaign_id':'d'*64}]:
  batch=dict(request['reference_batch']);batch['batch_id']=uuid.uuid4().hex
  value=json.loads(agentc._native.optimize_record_provider_receipt(json.dumps({'operation':'begin_batch','batch':batch})))
  assert value['status']=='recorded',value
  admission=dict(p['independent']);admission.pop('campaign')
  deployment={'preparation':{'batch_id':batch['batch_id'],'max_workers':1,'template_execution_id':p['template_id'],'input_sha256':'d'*64},'admission':admission,'dynamic_primary_nodes':True}
  if replacement is not None:deployment['campaign']=replacement
  value=json.loads(agentc._native.optimize_record_provider_receipt(json.dumps({'operation':'begin_graph_deployment','request':deployment})))
  assert value['status']=='abstained' and 'original campaign commitment' in value['reason'],value
 assert ProviderReceiptBatch.graph_campaign_report(p['campaign']['campaign_id'])==p['final']
finally:agentc.shutdown()
""", str(proof_path)], env=env, capture_output=True, text=True, timeout=90)
    assert restart.returncode == 0, restart.stderr
    audit = subprocess.run([sys.executable, "-I", "-c", r"""
import sqlite3,json,hashlib,sys
from pathlib import Path
assert not any(n=='agentc' or n.startswith('agentc.') for n in sys.modules)
p=Path(sys.argv[1]);proof=json.loads(p.read_text());c=sqlite3.connect(p.parent/'cost_model.db');fault=proof['fault']
assert c.execute('SELECT COUNT(*) FROM provider_receipt_invocation').fetchone()[0]==proof['sdk_sends']==(82 if fault else 86)
assert c.execute('SELECT COUNT(*) FROM provider_graph_campaign_invocation').fetchone()[0]==proof['sdk_sends']
assert c.execute('SELECT COUNT(*) FROM provider_graph_campaign_workflow').fetchone()[0]==(22 if fault else 24)
assert c.execute('SELECT COUNT(*) FROM provider_graph_sampling').fetchone()[0]==(21 if fault else 22)
assert c.execute('SELECT COUNT(*) FROM provider_graph_deployment').fetchone()[0]==(1 if fault else 3)
assert c.execute('SELECT COUNT(*) FROM provider_graph_pair').fetchone()[0]==20
for table,hcol,jcol in [('provider_graph_campaign','policy_sha256','policy_json'),('provider_graph_campaign_workflow','enrollment_sha256','enrollment_json')]:
 for h,raw in c.execute('SELECT '+hcol+','+jcol+' FROM '+table):assert hashlib.sha256(raw.encode()).hexdigest()==h
for h,raw in c.execute('SELECT i.invocation_sha256,r.invocation_json FROM provider_graph_campaign_invocation i JOIN provider_receipt_invocation r USING(invocation_id)'):
 assert hashlib.sha256(raw.encode()).hexdigest()==h
assert c.execute('SELECT COUNT(*) FROM provider_receipt_invocation WHERE json_extract(completion_json,"$.status")="failed"').fetchone()[0]==int(fault)
assert not c.execute('PRAGMA foreign_key_check').fetchall()
""", str(proof_path)], capture_output=True, text=True, timeout=30)
    assert audit.returncode == 0, audit.stderr


CAMPAIGN_MONEY_SDK = COMMITMENT_SAMPLING_SDK.split("    with agentc.application_version('commitment-sdk-v1')")[0] + r"""
    from agentc._provider_receipts import ProviderReceiptBatch
    def driver_a(item):return task(item)
    def driver_b(item):return task(item)
    with agentc.application_version('campaign-money-sdk-v1'),agentc.optimization_scope('campaign-money-sdk',optimize=True):
        campaign={'campaign_id':'c'*64,'commitments':{'money':money,'max_open_workflows':1,'max_pending_invocations':1},'max_workflows':100,'max_physical_invocations':4,'max_lifetime_physical_invocations':1000}
        learning={'policy':{'sampling_rate':1.,'max_workflows':100,'max_samples':100,'max_physical_invocations':4,'campaign':campaign,'replay_contract_sha256':'a'*64}}
        observed=[]
        for n in range(12):
            before=(len(sends),len(callbacks))
            try:result=agentc.parallel_map(driver_a if n%2==0 else driver_b,['INPUT_'+str(n)],comparison='graph',graph_sampling=learning,on_observation=observed.append)
            except RuntimeError as exc:
                assert 'campaign' in str(exc),exc
                assert before==(len(sends),len(callbacks));break
            assert result==['PRIVATE_COMMIT_RESULT'],result
            report=observed[-1].graph_sampling
            if fault and n==0:assert report['completion']['status']=='incomplete',report
            else:assert report['completion']['status']=='paired',report
        else:raise AssertionError('multiple drivers replenished shared lifetime money')
        final=ProviderReceiptBatch.graph_campaign_report(campaign['campaign_id'])
        assert final['workflows']==len(observed)>=2 and final['physical_invocations']==len(sends)==4*len(observed),final
        assert final['held_workflows']==final['unknown_cost_workflows']==int(fault) and final['pending_invocations']==final['open_workflows']==0,final
        assert len({o.graph_sampling['ticket']['driver_id'] for o in observed})==2
        changed=json.loads(json.dumps(learning));changed['policy']['campaign']['commitments']['money']['lifetime_usd' if quantity=='billed_usd' else 'lifetime_nanodollars']*=2
        before=(len(sends),len(callbacks))
        try:agentc.parallel_map(driver_b,['REPLENISH'],comparison='graph',graph_sampling=changed)
        except RuntimeError:pass
        else:raise AssertionError('changed driver policy replenished shared campaign money')
        assert before==(len(sends),len(callbacks))
        proof={'quantity':quantity,'mode':mode,'fault':fault,'sdk_sends':len(sends),'callbacks':len(callbacks),'campaign':campaign,'final':final,'reports':[o.graph_sampling for o in observed]}
        assert 'PRIVATE_' not in json.dumps(proof);print(json.dumps(proof))
finally:
    if scope:scope.__exit__(None,None,None)
    agentc.shutdown()
    if mode=='async':asyncio.run(client.close())
    else:client.close()
"""


@pytest.mark.parametrize("quantity", ["billed_usd", "published_token_tariff_usd"])
@pytest.mark.parametrize("fault", [False, True])
@pytest.mark.parametrize("mode", ["sync", "async"])
def test_shared_graph_campaign_sdk_multiple_drivers_cannot_replenish_money(tmp_path, quantity, fault, mode):
    env = {**os.environ, "AGENTC_STORAGE_PATH": str(tmp_path), "AGENTC_OPTIMIZE": "1",
           "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": "1000", "AGENTC_OPTIMIZE_EXPLORATION": "1",
           "AGENTC_OPTIMIZE_SHADOW": "1", "AGENTC_CAPTURE_CONTENT": "0", "AGENTC_CAPTURE_EMBEDDINGS": "0",
           "AGENTC_GRAPH_COMMIT_QUANTITY": quantity, "AGENTC_GRAPH_COMMIT_MODE": mode,
           "AGENTC_GRAPH_COMMIT_FAULT": str(int(fault))}
    env.pop("AGENTC_EVAL_PLANNER_MODE", None)
    result = subprocess.run([sys.executable, "-I", "-c", CAMPAIGN_MONEY_SDK], env=env, capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stderr
    proof_path = tmp_path / "provider-graph-campaign-money-sdk-proof.json"
    proof_path.write_text(result.stdout)
    restart = subprocess.run([sys.executable, "-I", "-c", r"""
import agentc,json,sys,uuid
from pathlib import Path
from agentc._provider_receipts import ProviderReceiptBatch
p=json.loads(Path(sys.argv[1]).read_text());agentc.init()
try:
 assert ProviderReceiptBatch.graph_campaign_report(p['campaign']['campaign_id'])==p['final']
 for original in p['reports']:
    request=json.loads(json.dumps(original['ticket']['request']))
    request['reference_batch']['batch_id']=uuid.uuid4().hex;request['candidate_batch']['batch_id']=uuid.uuid4().hex
    result=json.loads(agentc._native.optimize_provider_graph_sampling(json.dumps({'operation':'begin','request':request})))
    assert result['status']=='abstained' and 'campaign' in result['reason'],result
finally:agentc.shutdown()
""", str(proof_path)], env=env, capture_output=True, text=True, timeout=60)
    assert restart.returncode == 0, restart.stderr
    audit = subprocess.run([sys.executable, "-I", "-c", r"""
import sqlite3,json,hashlib,sys
from pathlib import Path
assert not any(n=='agentc' or n.startswith('agentc.') for n in sys.modules)
p=Path(sys.argv[1]);proof=json.loads(p.read_text());c=sqlite3.connect(p.parent/'cost_model.db');n=len(proof['reports'])
assert c.execute('SELECT COUNT(*) FROM provider_graph_sampling_state').fetchone()[0]==2
assert c.execute('SELECT COUNT(*) FROM provider_graph_sampling').fetchone()[0]==n
assert c.execute('SELECT COUNT(*) FROM provider_graph_campaign_workflow').fetchone()[0]==n
assert c.execute('SELECT COUNT(*) FROM provider_receipt_invocation').fetchone()[0]==proof['sdk_sends']==4*n
assert c.execute('SELECT COUNT(*) FROM provider_graph_campaign_invocation').fetchone()[0]==proof['sdk_sends']
assert c.execute('SELECT COUNT(*) FROM provider_graph_pair').fetchone()[0]==n-int(proof['fault'])
assert not c.execute('PRAGMA foreign_key_check').fetchall()
""", str(proof_path)], capture_output=True, text=True, timeout=30)
    assert audit.returncode == 0, audit.stderr
