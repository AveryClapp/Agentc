"""Retained SDK quality evidence survives billing and publication failures."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


SDK_WORKER = r"""
import hashlib,json,os,sys,time
from dataclasses import asdict
from pathlib import Path
import agentc,httpx,openai
root=Path(os.environ['AGENTC_STORAGE_PATH']);phase=sys.argv[1];fault=os.environ['DEFERRED_FAULT']
sends=[];gets=[]
def handler(request):
    if request.method=='GET':
        assert phase=='bill', 'recovery must not repeat a lookup'
        generation=request.url.params['id'];gets.append(generation)
        return httpx.Response(200,json={'data':{'id':generation,'request_id':'req-'+generation,'model':'openai/gpt-4o-mini','native_tokens_prompt':2,'native_tokens_completion':1,'total_cost':.02,'is_byok':False,'cancelled':False,'finish_reason':'stop'}})
    assert phase=='execute', 'publication must not replay an SDK call'
    sends.append(1);generation='gen-deferred-'+str(len(sends));time.sleep(.005)
    content='PRIVATE_DEFERRED_ALTERNATE' if fault=='divergent' and len(sends)>2 else 'PRIVATE_DEFERRED_OUTPUT'
    return httpx.Response(200,headers={'x-request-id':'req-'+generation},json={'id':generation,'object':'chat.completion','created':0,'model':'openai/gpt-4o-mini','choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':content}}],'usage':{'prompt_tokens':2,'completion_tokens':1,'total_tokens':3}})
client=openai.OpenAI(api_key='PRIVATE_DEFERRED_KEY',base_url='https://openrouter.ai/api/v1',max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(handler)))
agentc.init()
if phase=='initialize':
    assert agentc.provider_original_execution('0'*32) is None
    agentc.shutdown();client.close();sys.exit(0)
if phase=='execute':
    observations=[];raws=[];original=agentc._native.optimize_observe_multi_call
    def capture(raw):
        raws.append(raw);return original(raw)
    agentc._native.optimize_observe_multi_call=capture
    requests=[agentc.CompletionRequest('openai',{'model':'openai/gpt-4o-mini','messages':[{'role':'user','content':'PRIVATE_DEFERRED_PROMPT_'+str(i)}],'max_tokens':8},client) for i in range(2)]
    with agentc.application_version('deferred-billing-v1'):
        for mode in ['reference','candidate']:
            results=agentc.completion_batch(requests,site='deferred',policy=agentc.BatchPolicy(10000),mode=mode,max_workers=2,on_observation=observations.append)
            expected_content='PRIVATE_DEFERRED_ALTERNATE' if fault=='divergent' and mode=='candidate' else 'PRIVATE_DEFERRED_OUTPUT'
            assert [row.choices[0].message.content for row in results]==[expected_content]*2
    assert len(sends)==4 and not gets
    expected='abstained' if fault=='checkpoint' else 'pending_billing'
    assert [row.status for row in observations]==[expected]*2,observations
    assert agentc.compare_parallel_observations(*observations).status=='abstained'
    if fault!='checkpoint':
        for raw in raws:
            for field in ['wall_latency_ms','output_fingerprint','runtime']:
                edited=json.loads(raw)
                if field=='wall_latency_ms':edited[field]+=1
                elif field=='output_fingerprint':edited['comparison']['output_sha256']='f'*64
                else:edited['calls'][0]['outcome']['executed_model_id']='openai/foreign-model'
                assert json.loads(original(json.dumps(edited)))['status']=='abstained',field
    proof={'observations':[asdict(row) for row in observations],'sdk_sends':4,'metadata_gets':0}
    if fault!='checkpoint':
        executions=[agentc.provider_original_execution(row.provider_receipt_batch_id) for row in observations]
        assert all(executions) and executions[0]['execution_id']!=executions[1]['execution_id']
        original_pair=None
        if fault!='usd_first':
            original_pair=agentc.compare_original_provider_executions(*(row['execution_id'] for row in executions))
            assert original_pair['divergence']==(1 if fault=='divergent' else 0)
            assert agentc.compare_original_provider_executions(*(row['execution_id'] for row in executions))==original_pair
        proof.update(original_executions=executions,original_pair=original_pair)
    (root/'deferred-proof.json').write_text(json.dumps(proof))
elif phase=='bill':
    proof=json.loads((root/'deferred-proof.json').read_text())
    for observation in proof['observations']:
        assert agentc.refresh_parallel_observation(agentc.ParallelObservation(**observation)).status==observation['status']
        for receipt in agentc.provider_billing_receipts(observation['provider_receipt_batch_id']):
            # The raw IDs remain caller-owned; the journal stores their hashes.
            generation=next('gen-deferred-'+str(i) for i in range(1,5) if hashlib.sha256(('provider-identity-v1\0gen-deferred-'+str(i)).encode()).hexdigest()==receipt.generation_sha256)
            result=agentc.reconcile_openrouter_generation(receipt,generation_id=generation,client=client)
            assert result.status==('abstained' if fault=='billing' else 'recorded'),result
    assert len(gets)==4 and not sends
    proof['metadata_gets']=len(gets);(root/'deferred-proof.json').write_text(json.dumps(proof))
    if fault=='committed':
        for row in proof['observations']:
            assert agentc.refresh_parallel_observation(agentc.ParallelObservation(**row)).status=='recorded'
        os._exit(79)
    if fault in {'none','divergent','usd_first'}:
        for row in proof['observations']:
            assert agentc.refresh_parallel_observation(agentc.ParallelObservation(**row)).status=='recorded'
    elif fault not in {'billing','checkpoint'}:
        for row in proof['observations']:
            unresolved=agentc.refresh_parallel_observation(agentc.ParallelObservation(**row))
            assert unresolved.status=='pending_billing' and unresolved.reason=='native_publication_unavailable',unresolved
elif phase=='recover':
    proof=json.loads((root/'deferred-proof.json').read_text())
    observations=[agentc.refresh_parallel_observation(agentc.ParallelObservation(**row)) for row in proof['observations']]
    if fault=='checkpoint':
        assert all(row.status=='abstained' for row in observations)
        assert agentc.compare_parallel_observations(*observations).status=='abstained'
    else:
        assert all(row.status=='recorded' and row.cost_usd==.04 and row.cost_source=='provider_billing_verified' for row in observations),observations
        assert all(row.wall_latency_ms==prior['wall_latency_ms'] for row,prior in zip(observations,proof['observations']))
        assert [agentc.provider_original_execution(row.provider_receipt_batch_id) for row in observations]==proof['original_executions']
        pair=agentc.compare_parallel_observations(*observations)
        assert pair.status in {'recorded','already_recorded'} and pair.divergence==(1 if fault=='divergent' else 0),pair
        assert agentc.compare_parallel_observations(*observations).status=='already_recorded'
        if proof['original_pair'] is None:
            proof['original_pair']=agentc.compare_original_provider_executions(*(row['execution_id'] for row in proof['original_executions']))
            (root/'deferred-proof.json').write_text(json.dumps(proof))
        assert agentc.compare_original_provider_executions(*(row['execution_id'] for row in proof['original_executions']))==proof['original_pair']
        (root/'deferred-recovered.json').write_text(json.dumps({'observations':[asdict(row) for row in observations],'pair':asdict(pair)}))
    for row in proof['observations']:
        for receipt in agentc.provider_billing_receipts(row['provider_receipt_batch_id']):
            # Cached reconciliation must not issue another GET.
            import hashlib
            generation=next('gen-deferred-'+str(i) for i in range(1,5) if hashlib.sha256(('provider-identity-v1\0gen-deferred-'+str(i)).encode()).hexdigest()==receipt.generation_sha256)
            assert agentc.reconcile_openrouter_generation(receipt,generation_id=generation,client=client).status=='already_recorded'
    assert not sends and not gets
agentc.shutdown();client.close()
"""


SQL_WORKER = r"""
import hashlib,json,sqlite3,sys
from pathlib import Path
assert not any(name=='agentc' or name.startswith('agentc.') for name in sys.modules)
root=Path(sys.argv[1]);phase=sys.argv[2];fault=sys.argv[3];conn=sqlite3.connect(root/'cost_model.db')
if phase=='inject':
    if fault=='checkpoint':target='BEFORE INSERT ON provider_receipt_publication_input'
    elif fault=='billing':target='BEFORE INSERT ON provider_billing_settlement'
    elif fault in {'none','divergent','committed','usd_first'}:target=None
    elif fault=='provider_receipt_profile_publication':target='BEFORE UPDATE OF result_json ON '+fault+' WHEN NEW.result_json IS NOT NULL'
    else:
        target='BEFORE INSERT ON '+fault
        if fault in {'execution_plan_profile','execution_plan_observation'}:target+=" WHEN NEW.provider_protocol='agentc.multi-call-runtime.v1'"
    if target:conn.execute('CREATE TRIGGER deferred_failure '+target+" BEGIN SELECT RAISE(ABORT,'deferred publication interrupted'); END;")
elif phase=='clear':
    conn.execute('DROP TRIGGER IF EXISTS deferred_failure')
else:
    proof=json.loads((root/'deferred-proof.json').read_text())
    journal=list(conn.execute('SELECT invocation_json,completion_json FROM provider_receipt_invocation ORDER BY item,logical_call,invocation_id'))
    assert len(journal)==4 and sum(json.loads(row[1])['http_attempts'] for row in journal)==proof['sdk_sends']==4
    assert all(json.loads(row[1])['cost_source']=='estimated' for row in journal)
    scalar=list(conn.execute("SELECT * FROM execution_plan_observation WHERE provider_protocol!='agentc.multi-call-runtime.v1' ORDER BY call_site_version,execution_plan_id,sample_sequence"))
    assert len(scalar)==4
    for raw, in conn.execute("SELECT cost_evidence_json FROM execution_plan_observation WHERE provider_protocol!='agentc.multi-call-runtime.v1'"):
        evidence=json.loads(raw)
        assert evidence['source']=='estimated' and len(evidence['physical_invocations'])==1,evidence
        identity=evidence['physical_invocations'][0]
        assert conn.execute('SELECT COUNT(*) FROM provider_receipt_invocation WHERE batch_id=? AND invocation_id=?',(identity['batch_id'],identity['invocation_id'])).fetchone()[0]==1
    assert conn.execute("SELECT COUNT(*) FROM execution_plan_observation WHERE provider_protocol!='agentc.multi-call-runtime.v1' AND cost_usd=.02").fetchone()[0]==0
    derived=[conn.execute(query).fetchone()[0] for query in ["SELECT COUNT(*) FROM execution_plan_observation WHERE provider_protocol='agentc.multi-call-runtime.v1'","SELECT COUNT(*) FROM multi_call_comparison_execution","SELECT COUNT(*) FROM multi_call_comparison_pair"]]
    checkpoint=list(conn.execute('SELECT input_json FROM provider_receipt_publication_input'))
    assert len(checkpoint)==(0 if fault=='checkpoint' else 2)
    for (raw,) in checkpoint:
        evidence=json.loads(raw)['deferred_evidence']
        assert len(evidence['scalar_provenance'])==2 and all(row['cost_source']=='estimated' and row['observation'] is not None for row in evidence['scalar_provenance'])
        for marker in ['PRIVATE_DEFERRED','api_key','authorization','gen-deferred-','req-gen-deferred-']:assert marker not in raw
    snapshot={'journal':journal,'scalar':scalar,'derived':derived}
    executions=list(conn.execute('SELECT execution_id,execution_json FROM provider_original_execution ORDER BY source_sequence'))
    originals=list(conn.execute('SELECT pair_id,pair_json FROM provider_original_execution_pair'))
    assert len(executions)==(0 if fault=='checkpoint' else 2)
    assert len(originals)==(0 if fault=='checkpoint' or (fault=='usd_first' and phase=='before') else 1)
    if fault!='checkpoint':
        assert [json.loads(raw) for _,raw in executions]==proof['original_executions']
        if originals:assert json.loads(originals[0][1])==proof['original_pair']
    snapshot.update(original_executions=executions,original_pairs=originals)
    if phase=='before':
        assert derived==[0,0,0]
        (root/'deferred-before.json').write_text(json.dumps(snapshot))
    elif phase=='after':
        before=json.loads((root/'deferred-before.json').read_text())
        assert json.loads(json.dumps(journal))==before['journal'] and json.loads(json.dumps(scalar))==before['scalar']
        assert json.loads(json.dumps(executions))==before['original_executions']
        if fault!='usd_first':assert json.loads(json.dumps(originals))==before['original_pairs']
        assert derived==([0,0,0] if fault=='checkpoint' else [2,2,1]),derived
        known,unknown,pending=conn.execute('SELECT known_cost_usd,unknown_cost_invocations,pending_billing_verifications FROM provider_economic_cohort').fetchone()
        assert abs(known-.08)<1e-12 and unknown==pending==0
        assert conn.execute('SELECT COUNT(*) FROM provider_billing_attempt').fetchone()[0]==proof['metadata_gets']==4
        assert conn.execute('SELECT COUNT(*) FROM provider_billing_settlement').fetchone()[0]==4
        if fault!='checkpoint':
            mappings=list(conn.execute('SELECT token,execution_id FROM provider_original_usd_comparison'))
            assert len(mappings)==2 and {row[1] for row in mappings}=={row[0] for row in executions}
            for token,execution_id in mappings:
                binding=json.loads(conn.execute('SELECT binding_json FROM multi_call_comparison_execution WHERE token=?',(token,)).fetchone()[0])
                execution=next(json.loads(raw) for identity,raw in executions if identity==execution_id)
                original=execution['original_comparison']
                assert binding['input_fingerprint']==original['input_fingerprint'] and binding['output_fingerprint']==original['output_fingerprint']
            assert conn.execute('SELECT observed_at FROM multi_call_comparison_pair').fetchone()[0]==proof['original_pair']['observed_at_us']
            for raw, in conn.execute("SELECT cost_evidence_json FROM execution_plan_observation WHERE provider_protocol='agentc.multi-call-runtime.v1'"):
                evidence=json.loads(raw)
                assert evidence['source']=='provider_billing_verified' and len(evidence['physical_invocations'])==2
            executions,cost=conn.execute('SELECT SUM(completed_executions),SUM(total_cost_usd) FROM multi_call_comparison_spend').fetchone()
            assert executions==2 and abs(cost-.08)<1e-12
        audit={'sdk_imported':False,'sdk_sends':4,'metadata_gets':4,'scalar_executions':4,'batch_executions':derived[0],'comparison_pairs':derived[2],'known_fixture_cost_usd':known,'journal_sha256':hashlib.sha256(json.dumps(journal).encode()).hexdigest()}
        (root/'deferred-audit.json').write_text(json.dumps(audit))
conn.commit();conn.close()
"""


def _run(script: str, root: Path, fault: str, *args: str, exit_code: int = 0):
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, *args],
        env={
            **os.environ,
            "AGENTC_STORAGE_PATH": str(root),
            "DEFERRED_FAULT": fault,
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
    assert result.returncode == exit_code, result.stderr


@pytest.mark.parametrize(
    "fault",
    [
        "checkpoint",
        "billing",
        "none",
        "divergent",
        "usd_first",
        "execution_plan_profile",
        "execution_plan_observation",
        "multi_call_comparison_spend",
        "multi_call_comparison_execution",
        "provider_original_usd_comparison",
        "provider_receipt_profile_publication",
        "committed",
    ],
)
def test_original_unbilled_sdk_batches_publish_once_after_billing_and_restart(
    tmp_path, fault
):
    _run(SDK_WORKER, tmp_path, fault, "initialize")
    _run(SQL_WORKER, tmp_path, fault, str(tmp_path), "inject", fault)
    _run(SDK_WORKER, tmp_path, fault, "execute")
    _run(SQL_WORKER, tmp_path, fault, str(tmp_path), "before", fault)
    _run(
        SDK_WORKER, tmp_path, fault, "bill", exit_code=79 if fault == "committed" else 0
    )
    _run(SQL_WORKER, tmp_path, fault, str(tmp_path), "clear", fault)
    _run(SDK_WORKER, tmp_path, fault, "recover")
    _run(SQL_WORKER, tmp_path, fault, str(tmp_path), "after", fault)
    audit = (tmp_path / "deferred-audit.json").read_bytes()
    _run(SDK_WORKER, tmp_path, fault, "recover")
    _run(SQL_WORKER, tmp_path, fault, str(tmp_path), "after", fault)
    assert (tmp_path / "deferred-audit.json").read_bytes() == audit
    assert json.loads(audit)["sdk_imported"] is False


@pytest.mark.parametrize("retirement", ["none", "pair", "binding"])
def test_completed_publications_recover_original_links_without_repairing_retired_quality(
    tmp_path, retirement
):
    _run(SDK_WORKER, tmp_path, "none", "initialize")
    _run(SDK_WORKER, tmp_path, "none", "execute")
    _run(SDK_WORKER, tmp_path, "none", "bill")
    _run(SDK_WORKER, tmp_path, "none", "recover")
    migration = r"""
import json,sqlite3,sys
from pathlib import Path
assert not any(n=='agentc' or n.startswith('agentc.') for n in sys.modules)
root=Path(sys.argv[1]);retirement=sys.argv[2];conn=sqlite3.connect(root/'cost_model.db')
for table in ['provider_original_execution_pair','provider_original_usd_comparison','provider_original_execution_state','provider_original_execution']:
    conn.execute('DROP TABLE '+table)
if retirement=='pair':conn.execute('DELETE FROM multi_call_comparison_pair')
if retirement=='binding':conn.execute('DELETE FROM multi_call_comparison_execution')
tables=['provider_receipt_invocation','provider_receipt_publication_input','provider_receipt_profile_publication',
    'multi_call_comparison_execution','multi_call_comparison_pair','multi_call_comparison_spend',
    'provider_billing_settlement','provider_economic_cohort','execution_plan_profile','execution_plan_observation']
snapshot={table:list(conn.execute('SELECT * FROM '+table+' ORDER BY 1,2')) for table in tables}
(root/'upgrade-original-snapshot.json').write_text(json.dumps(snapshot))
conn.commit();conn.close()
"""
    _run(migration, tmp_path, "none", str(tmp_path), retirement)
    recovery = r"""
import agentc,json,sys
from pathlib import Path
root=Path(sys.argv[1]);retirement=sys.argv[2];proof=json.loads((root/'deferred-proof.json').read_text())
agentc.init()
try:
    try:
        executions=[agentc.provider_original_execution(row['batch_id']) for row in proof['original_executions']]
        pair=agentc.compare_original_provider_executions(*(row['execution_id'] for row in executions))
    except RuntimeError:
        assert retirement!='none'
    else:
        assert retirement=='none' and executions==proof['original_executions'] and pair==proof['original_pair']
        assert agentc.compare_original_provider_executions(*(row['execution_id'] for row in executions))==pair
finally:
    agentc.shutdown()
"""
    _run(recovery, tmp_path, "none", str(tmp_path), retirement)
    audit = r"""
import json,sqlite3,sys
from pathlib import Path
assert not any(n=='agentc' or n.startswith('agentc.') for n in sys.modules)
root=Path(sys.argv[1]);retirement=sys.argv[2];conn=sqlite3.connect(root/'cost_model.db')
before=json.loads((root/'upgrade-original-snapshot.json').read_text())
for table,rows in before.items():
    assert json.loads(json.dumps(list(conn.execute('SELECT * FROM '+table+' ORDER BY 1,2'))))==rows,table
assert conn.execute('SELECT COUNT(*) FROM provider_original_execution_pair').fetchone()[0]==(1 if retirement=='none' else 0)
if retirement=='binding':
    assert conn.execute('SELECT COUNT(*) FROM provider_original_execution').fetchone()[0]==0
    assert conn.execute('SELECT COUNT(*) FROM provider_original_execution_state').fetchone()[0]==0
    assert conn.execute('SELECT COUNT(*) FROM provider_original_usd_comparison').fetchone()[0]==0
"""
    _run(audit, tmp_path, "none", str(tmp_path), retirement)
    _run(recovery, tmp_path, "none", str(tmp_path), retirement)
    _run(audit, tmp_path, "none", str(tmp_path), retirement)
