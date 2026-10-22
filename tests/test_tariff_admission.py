"""Original SDK evidence drives explicit tariff admission without USD invoices."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest


SDK_WORKER = r"""
import agentc,openai,anthropic,httpx,json,time,os
from dataclasses import asdict
from pathlib import Path
sends=[];altered=False
def handler(request):
    sends.append(request.url.path);time.sleep(.015)
    if request.url.path.endswith('chat/completions'):
        body={'id':'chatcmpl-fixture','object':'chat.completion','created':0,'model':'gpt-4o-mini-2024-07-18','service_tier':'default',
            'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':'PRIVATE_ADMISSION_ALTERNATE' if altered else 'PRIVATE_ADMISSION_RESULT'}}],
            'usage':{'prompt_tokens':10000,'completion_tokens':1000,'total_tokens':11000,'prompt_tokens_details':{'cached_tokens':4096}}}
    else:
        body={'id':'msg_fixture','type':'message','role':'assistant','model':'claude-haiku-4-5-20251001',
            'content':[{'type':'text','text':'PRIVATE_ADMISSION_ALTERNATE' if altered else 'PRIVATE_ADMISSION_RESULT'}],'stop_reason':'end_turn','stop_sequence':None,
            'usage':{'input_tokens':10000,'output_tokens':1000,'cache_creation_input_tokens':5000,'cache_read_input_tokens':20000,
                'service_tier':'standard','inference_geo':'global'}}
    return httpx.Response(200,json=body)
agentc.init()
clients=[openai.OpenAI(api_key='fixture',max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(handler))),
    anthropic.Anthropic(api_key='fixture',max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(handler)))]
try:
    executions=[];pairs=[];schedules=[]
    with agentc.application_version('tariff-admission-sdk-v1'),agentc.tariff_accounting():
        requests=[agentc.CompletionRequest(provider,{'model':model,'messages':[{'role':'user','content':'PRIVATE_ADMISSION_PROMPT'}],
                'max_tokens':2048},client)
            for provider,model,client in zip(['openai','anthropic'],
                ['gpt-4o-mini-2024-07-18','claude-haiku-4-5-20251001'],clients)]
        for run in range(20):
            current=[]
            for mode in ['reference','candidate']:
                observations=[]
                agentc.completion_batch(requests,site='tariff-admission',policy=agentc.BatchPolicy(10000),mode=mode,max_workers=2,
                    on_observation=observations.append)
                assert observations[0].status=='pending_billing',observations
                current.append(agentc.provider_original_execution(observations[0].provider_receipt_batch_id))
            pair=agentc.compare_original_provider_executions(*(row['execution_id'] for row in current))
            assert pair['divergence']==0
            quality=agentc.provider_original_quality_report(current[1]['batch_id'])['quality_window']
            assert quality['lifetime_pairs']==len(quality['pairs'])==run+1 and quality['divergence_exposure']==0
            executions.extend(current);pairs.append(pair)
        assert len(sends)==80
        tariff=dict(economic_quantity='published_token_tariff_usd',max_expected_tariff_nanodollars=100_000_000)
        policies=[agentc.BatchPolicy(10000),agentc.BatchPolicy(1,**tariff),
            agentc.BatchPolicy(10000,economic_quantity='published_token_tariff_usd',max_expected_tariff_nanodollars=1),
            agentc.BatchPolicy(10000,objective='cost',**tariff),agentc.BatchPolicy(10000,**tariff)]
        for index,policy in enumerate(policies):
            observed=[];selected=[]
            agentc.completion_batch(requests,site='tariff-admission',policy=policy,mode='guarded',max_workers=2,
                on_observation=observed.append,on_schedule=selected.append)
            assert len(selected)==1 and observed[0].status=='pending_billing'
            schedule=selected[0]
            assert schedule.selected_reference==(index<4),(index,schedule)
            assert schedule.max_workers==(1 if index<4 else 2),(index,schedule)
            assert schedule.economic_quantity==policy.economic_quantity
            if index==0:assert schedule.reason=='acquisition_costs_incomplete'
            else:
                assert schedule.acquisition_tariff_complete and schedule.acquisition_upper_tariff_nanodollars==1_151_712_000,schedule
            schedules.append(asdict(schedule))
        assert len(sends)==90
        adverse=[]
        for mode in ['reference','candidate']:
            altered=mode=='candidate'
            observations=[]
            agentc.completion_batch(requests,site='tariff-admission',policy=agentc.BatchPolicy(10000),mode=mode,max_workers=2,on_observation=observations.append)
            adverse.append(agentc.provider_original_execution(observations[0].provider_receipt_batch_id))
        bad=agentc.compare_original_provider_executions(*(row['execution_id'] for row in adverse))
        assert bad['divergence']==1
        selected=[]
        agentc.completion_batch(requests,site='tariff-admission',policy=agentc.BatchPolicy(10000,**tariff),mode='guarded',max_workers=2,on_schedule=selected.append)
        assert len(sends)==96 and selected[0].selected_reference and selected[0].max_workers==1,selected
        quality=agentc.provider_original_quality_report(adverse[-1]['batch_id'])
        assert quality['quality_window']['lifetime_pairs']==21 and quality['quality_window']['pairs']==[]
        assert quality['quality_window']['divergence_exposure']==1 and quality['quality_window']['disabled']
        assert agentc.compare_original_provider_executions(*(row['execution_id'] for row in executions[-2:]))==pairs[-1]
        proof=dict(sdk_sends=len(sends),executions=executions+adverse,pairs=pairs+[bad],schedules=schedules+[asdict(selected[0])],quality=quality)
        assert 'PRIVATE_ADMISSION_' not in json.dumps(proof)
        print(json.dumps(proof))
finally:
    agentc.shutdown()
    for client in clients:client.close()
"""


def test_real_mixed_sdk_tariff_admission_keeps_default_usd_held(tmp_path):
    env = {
        **os.environ,
        "AGENTC_STORAGE_PATH": str(tmp_path),
        "AGENTC_OPTIMIZE": "1",
        "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": "1000",
        "AGENTC_OPTIMIZE_PLAN_PROFILE_WINDOW": "50",
        "AGENTC_OPTIMIZE_EXPLORATION": "0",
        "AGENTC_OPTIMIZE_SHADOW": "0",
        "AGENTC_CAPTURE_CONTENT": "0",
        "AGENTC_CAPTURE_EMBEDDINGS": "0",
    }
    result = subprocess.run(
        [sys.executable, "-I", "-c", SDK_WORKER],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    proof = json.loads(result.stdout)
    (tmp_path / "tariff-admission-proof.json").write_text(json.dumps(proof))
    audit_script = r"""
import json,sqlite3,sys
from pathlib import Path
assert not any(n=='agentc' or n.startswith('agentc.') for n in sys.modules)
conn=sqlite3.connect(Path(sys.argv[1])/'cost_model.db')
assert conn.execute('SELECT COUNT(*) FROM provider_receipt_invocation').fetchone()[0]==96
assert conn.execute('SELECT COUNT(*) FROM provider_original_execution_pair').fetchone()[0]==21
assert conn.execute('SELECT COUNT(*) FROM provider_original_quality_event').fetchone()[0]==21
assert conn.execute('SELECT SUM(lifetime_pairs) FROM provider_original_quality_state').fetchone()[0]==21
assert conn.execute('SELECT COUNT(*) FROM provider_original_execution').fetchone()[0]==48
assert conn.execute('SELECT COUNT(*) FROM provider_tariff_plan_observation').fetchone()[0]==48
assert conn.execute('SELECT upper_nanodollars FROM provider_tariff_cohort').fetchone()[0]==1_209_297_600
assert conn.execute('SELECT COUNT(*) FROM provider_billing_settlement').fetchone()[0]==0
assert conn.execute('SELECT COUNT(*) FROM multi_call_comparison_pair').fetchone()[0]==0
assert conn.execute('SELECT COUNT(*) FROM provider_original_usd_comparison').fetchone()[0]==0
assert conn.execute('SELECT COUNT(*) FROM execution_plan_observation').fetchone()[0]==96
assert all(json.loads(row[0])['source']=='estimated' for row in conn.execute('SELECT cost_evidence_json FROM execution_plan_observation'))
assert all(row[0]==0 for row in conn.execute('SELECT n_paired_observations FROM execution_plan_profile'))
print(json.dumps(dict(events=list(conn.execute('SELECT * FROM provider_original_quality_event ORDER BY candidate_source_sequence')),
    states=list(conn.execute('SELECT * FROM provider_original_quality_state ORDER BY execution_plan_id')))))
"""
    before = subprocess.run(
        [sys.executable, "-I", "-c", audit_script, str(tmp_path)],
        capture_output=True, text=True, timeout=30,
    )
    assert before.returncode == 0, before.stderr
    restart_script = r"""
import agentc,json,sys
agentc.init()
try:print(json.dumps(agentc.provider_original_quality_report(sys.argv[1])))
finally:agentc.shutdown()
"""
    restart = subprocess.run(
        [sys.executable, "-I", "-c", restart_script, proof["executions"][-1]["batch_id"]],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert restart.returncode == 0, restart.stderr
    assert json.loads(restart.stdout) == proof["quality"]
    after = subprocess.run(before.args, capture_output=True, text=True, timeout=30)
    assert after.returncode == 0, after.stderr
    assert json.loads(after.stdout) == json.loads(before.stdout)


REPORTED_SDK_WORKER = r"""
import agentc,openai,httpx,json,time
from dataclasses import asdict
sends=[]
def handler(request):
    sends.append(1);time.sleep(.015)
    return httpx.Response(200,json={'id':'chatcmpl-reported','object':'chat.completion','created':0,
        'model':'gpt-4o-mini-2024-07-18','choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':'PRIVATE_REPORTED_RESULT'}}],
        'usage':{'prompt_tokens':2,'completion_tokens':1,'total_tokens':3,'cost':.02,'is_byok':False}})
client=openai.OpenAI(api_key='fixture',max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(handler)))
agentc.init()
try:
    executions=[]
    with agentc.application_version('reported-original-quality-sdk-v1'):
        requests=[agentc.CompletionRequest('openai',{'model':'gpt-4o-mini-2024-07-18',
            'messages':[{'role':'user','content':'PRIVATE_REPORTED_PROMPT_'+str(i)}],'max_tokens':16},client) for i in range(2)]
        for run in range(20):
            current=[]
            for mode in ['reference','candidate']:
                observations=[]
                agentc.completion_batch(requests,site='reported-quality',policy=agentc.BatchPolicy(10000),mode=mode,max_workers=2,on_observation=observations.append)
                assert observations[0].status=='recorded' and observations[0].cost_source=='provider_reported' and observations[0].cost_usd==.04,observations
                current.append(agentc.provider_original_execution(observations[0].provider_receipt_batch_id))
            assert agentc.compare_original_provider_executions(*(row['execution_id'] for row in current))['divergence']==0
            executions.extend(current)
        schedules=[];observations=[]
        agentc.completion_batch(requests,site='reported-quality',policy=agentc.BatchPolicy(10000),mode='guarded',max_workers=2,
            on_schedule=schedules.append,on_observation=observations.append)
        assert len(sends)==82 and len(schedules)==1 and schedules[0].max_workers==2,schedules
        assert schedules[0].economic_quantity=='billed_usd' and schedules[0].acquisition_cost_complete
        assert abs(schedules[0].completed_acquisition_cost_usd-1.6)<1e-12
        quality=agentc.provider_original_quality_report(executions[-1]['batch_id'])
        assert quality['quality_window']['lifetime_pairs']==len(quality['quality_window']['pairs'])==20
        print(json.dumps(dict(sdk_sends=len(sends),executions=executions,quality=quality,schedule=asdict(schedules[0]))))
finally:
    agentc.shutdown();client.close()
"""


def test_reported_sdk_costs_share_original_quality_with_default_billed_admission(tmp_path):
    env = {
        **os.environ,
        "AGENTC_STORAGE_PATH": str(tmp_path),
        "AGENTC_OPTIMIZE": "1",
        "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": "1000",
        "AGENTC_OPTIMIZE_PLAN_PROFILE_WINDOW": "50",
        "AGENTC_OPTIMIZE_EXPLORATION": "0",
        "AGENTC_OPTIMIZE_SHADOW": "0",
        "AGENTC_CAPTURE_CONTENT": "0",
        "AGENTC_CAPTURE_EMBEDDINGS": "0",
    }
    result = subprocess.run(
        [sys.executable, "-I", "-c", REPORTED_SDK_WORKER],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    proof = json.loads(result.stdout)
    (tmp_path / "reported-original-quality-proof.json").write_text(json.dumps(proof))
    audit_script = r"""
import json,sqlite3,sys
from pathlib import Path
assert not any(n=='agentc' or n.startswith('agentc.') for n in sys.modules)
conn=sqlite3.connect(Path(sys.argv[1])/'cost_model.db')
assert conn.execute('SELECT COUNT(*) FROM provider_receipt_invocation').fetchone()[0]==82
assert conn.execute('SELECT COUNT(*) FROM provider_original_execution').fetchone()[0]==41
assert conn.execute('SELECT COUNT(*) FROM provider_original_execution_pair').fetchone()[0]==20
assert conn.execute('SELECT COUNT(*) FROM provider_original_quality_event').fetchone()[0]==20
assert conn.execute('SELECT SUM(lifetime_pairs) FROM provider_original_quality_state').fetchone()[0]==20
assert conn.execute('SELECT COUNT(*) FROM multi_call_comparison_pair').fetchone()[0]==0
assert conn.execute('SELECT COUNT(*) FROM provider_billing_settlement').fetchone()[0]==0
assert conn.execute('SELECT COUNT(*) FROM provider_tariff_plan_observation').fetchone()[0]==0
for raw, in conn.execute('SELECT input_json FROM provider_receipt_publication_input'):
    source=json.loads(raw)
    assert source['original_evidence'] and 'deferred_evidence' not in source
    assert all(row['cost_source']=='provider_reported' for row in source['original_evidence']['scalar_provenance'])
assert conn.execute("SELECT COUNT(*) FROM execution_plan_observation WHERE provider_protocol='agentc.multi-call-runtime.v1'").fetchone()[0]==41
assert all(row[0]==0 for row in conn.execute('SELECT n_paired_observations FROM execution_plan_profile'))
assert all(json.loads(row[0])['source']=='provider_reported' for row in conn.execute('SELECT cost_evidence_json FROM execution_plan_observation'))
"""
    audit = subprocess.run(
        [sys.executable, "-I", "-c", audit_script, str(tmp_path)],
        capture_output=True, text=True, timeout=30,
    )
    assert audit.returncode == 0, audit.stderr


LEGACY_GUARD_SDK_WORKER = r"""
import agentc,openai,httpx,json,sys,time
from dataclasses import asdict
from pathlib import Path
sends=[]
def handler(request):
    sends.append(1);time.sleep(.015)
    usage={'prompt_tokens':2,'completion_tokens':1,'total_tokens':3,'prompt_tokens_details':{'cached_tokens':0}}
    if sys.argv[2]=='billed_usd':usage.update(cost=.02,is_byok=False)
    return httpx.Response(200,json={'id':'chatcmpl-guard-fixture','object':'chat.completion','created':0,
        'service_tier':'default',
        'model':'gpt-4o-mini-2024-07-18','choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':'PRIVATE_GUARD_RESULT'}}],
        'usage':usage})
client=openai.OpenAI(api_key='fixture',max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(handler)))
agentc.init()
try:
    with agentc.application_version('legacy-guard-original-sdk-v1'),agentc.tariff_accounting():
        requests=[agentc.CompletionRequest('openai',{'model':'gpt-4o-mini-2024-07-18',
            'messages':[{'role':'user','content':'PRIVATE_GUARD_PROMPT_'+str(i)}],'max_tokens':16},client) for i in range(2)]
        if sys.argv[1]=='acquire':
            for run in range(20):
                current=[];seen=[]
                for mode in ['reference','candidate']:
                    observed=[]
                    agentc.completion_batch(requests,site='legacy-guard',policy=agentc.BatchPolicy(100000),mode=mode,
                        max_workers=2,on_observation=observed.append)
                    seen.append(observed[0])
                    current.append(agentc.provider_original_execution(observed[0].provider_receipt_batch_id))
                assert agentc.compare_original_provider_executions(*(row['execution_id'] for row in current))['divergence']==0
                if len(sys.argv)>3 and sys.argv[3]=='legacy-quality':
                    assert agentc.compare_parallel_observations(*seen).status in {'recorded','already_recorded'}
            quality=agentc.provider_original_quality_report(current[-1]['batch_id'])
            assert quality['quality_window']['lifetime_pairs']==len(quality['quality_window']['pairs'])==20
            assert len(sends)==80
            print(json.dumps(quality))
        else:
            selected=[]
            agentc.completion_batch(requests,site='legacy-guard',policy=agentc.BatchPolicy(100000,economic_quantity=sys.argv[2]),
                mode='guarded',max_workers=2,on_schedule=selected.append)
            assert len(sends)==2 and len(selected)==1
            print(json.dumps(asdict(selected[0])))
finally:
    agentc.shutdown();client.close()
"""


@pytest.mark.parametrize("quantity", ["billed_usd", "published_token_tariff_usd"])
@pytest.mark.parametrize("legacy_runtime", ["current", "older"])
def test_restarted_sdk_guard_uses_verified_original_runtime(tmp_path, quantity, legacy_runtime):
    env = {
        **os.environ,
        "AGENTC_STORAGE_PATH": str(tmp_path),
        "AGENTC_OPTIMIZE": "1",
        "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": "1000",
        "AGENTC_OPTIMIZE_PLAN_PROFILE_WINDOW": "50",
        "AGENTC_OPTIMIZE_EXPLORATION": "0",
        "AGENTC_OPTIMIZE_SHADOW": "0",
        "AGENTC_CAPTURE_CONTENT": "0",
        "AGENTC_CAPTURE_EMBEDDINGS": "0",
    }
    acquired = subprocess.run(
        [sys.executable, "-I", "-c", LEGACY_GUARD_SDK_WORKER, "acquire", quantity],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert acquired.returncode == 0, acquired.stderr
    proof_path = tmp_path / "legacy-guard-original-proof.json"
    proof_path.write_text(acquired.stdout)
    seed_script = r"""
import json,sqlite3,sys,time
from pathlib import Path
assert not any(n=='agentc' or n.startswith('agentc.') for n in sys.modules)
proof=json.loads(Path(sys.argv[1]).read_text())['quality_window'];key=proof['key'];runtime=proof['runtime_version'].copy()
if sys.argv[2]=='older':runtime['target_model_version']='legacy-fixture-runtime'
now=time.time_ns()//1000
conn=sqlite3.connect(Path(sys.argv[1]).parent/'cost_model.db')
fields=(key['call_site_version'],key['execution_plan_id']);versions=tuple(runtime[f] for f in ['provider_protocol','target_model_id','target_model_version','price_table_version'])
conn.execute('INSERT INTO execution_plan_guard VALUES(?,?,0,0,0,?,?,?,?,?)',(*fields,*versions,now))
conn.execute('INSERT INTO execution_plan_disabled VALUES(?,?,?,0,?,?)',(*fields,'validation_rejections',now,now+86400000000))
for id in ['a'*32,'b'*32]:conn.execute('INSERT INTO execution_plan_validation_rejection VALUES(?,?,?,?,?,?,?,?)',(*fields,id,*versions,now))
conn.commit()
"""
    seeded = subprocess.run(
        [sys.executable, "-I", "-c", seed_script, str(proof_path), legacy_runtime],
        capture_output=True, text=True, timeout=30,
    )
    assert seeded.returncode == 0, seeded.stderr
    selected = subprocess.run(
        [sys.executable, "-I", "-c", LEGACY_GUARD_SDK_WORKER, "select", quantity],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert selected.returncode == 0, selected.stderr
    schedule = json.loads(selected.stdout)
    assert schedule["economic_quantity"] == quantity
    assert schedule["selected_reference"] == (legacy_runtime == "current"), schedule
    assert schedule["max_workers"] == (1 if legacy_runtime == "current" else 2), schedule
    (tmp_path / "legacy-guard-selection.json").write_text(selected.stdout)
    audit_script = r"""
import json,sqlite3,sys
from pathlib import Path
assert not any(n=='agentc' or n.startswith('agentc.') for n in sys.modules)
proof=json.loads(Path(sys.argv[1]).read_text())['quality_window'];key=proof['key'];fields=(key['call_site_version'],key['execution_plan_id'])
conn=sqlite3.connect(Path(sys.argv[1]).parent/'cost_model.db')
assert conn.execute('SELECT COUNT(*) FROM provider_receipt_invocation').fetchone()[0]==82
assert conn.execute('SELECT COUNT(*) FROM provider_original_execution').fetchone()[0]==41
assert conn.execute('SELECT COUNT(*) FROM provider_original_quality_event').fetchone()[0]==20
assert conn.execute('SELECT SUM(lifetime_pairs) FROM provider_original_quality_state').fetchone()[0]==20
assert conn.execute('SELECT COUNT(*) FROM multi_call_comparison_pair').fetchone()[0]==0
assert conn.execute('SELECT COALESCE(SUM(n_paired_observations),0) FROM execution_plan_profile').fetchone()[0]==0
assert conn.execute('SELECT COUNT(*) FROM pragma_foreign_key_check').fetchone()[0]==0
expected=1 if sys.argv[2]=='current' else 0
assert conn.execute('SELECT COUNT(*) FROM execution_plan_disabled WHERE call_site_version=? AND execution_plan_id=?',fields).fetchone()[0]==expected
assert conn.execute('SELECT COUNT(*) FROM execution_plan_validation_rejection WHERE call_site_version=? AND execution_plan_id=?',fields).fetchone()[0]==expected*2
stored=conn.execute('SELECT target_model_version FROM execution_plan_guard WHERE call_site_version=? AND execution_plan_id=?',fields).fetchone()[0]
assert stored==proof['runtime_version']['target_model_version']
spend=conn.execute("SELECT COALESCE(SUM(total_cost_usd),0) FROM multi_call_comparison_spend WHERE call_site_version=?",fields[:1]).fetchone()[0]
assert abs(spend-(1.6 if sys.argv[3]=='billed_usd' else 0))<1e-9
"""
    audited = subprocess.run(
        [sys.executable, "-I", "-c", audit_script, str(proof_path), legacy_runtime, quantity],
        capture_output=True, text=True, timeout=30,
    )
    assert audited.returncode == 0, audited.stderr


def test_sdk_window_change_cannot_reuse_retired_originals_through_legacy_quality(tmp_path):
    env = {
        **os.environ,
        "AGENTC_STORAGE_PATH": str(tmp_path),
        "AGENTC_OPTIMIZE": "1",
        "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": "1000",
        "AGENTC_OPTIMIZE_PLAN_PROFILE_WINDOW": "50",
        "AGENTC_OPTIMIZE_EXPLORATION": "0",
        "AGENTC_OPTIMIZE_SHADOW": "0",
        "AGENTC_CAPTURE_CONTENT": "0",
        "AGENTC_CAPTURE_EMBEDDINGS": "0",
    }
    acquired = subprocess.run(
        [sys.executable, "-I", "-c", LEGACY_GUARD_SDK_WORKER, "acquire", "billed_usd", "legacy-quality"],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert acquired.returncode == 0, acquired.stderr
    (tmp_path / "window-contract-original-proof.json").write_text(acquired.stdout)
    selected = subprocess.run(
        [sys.executable, "-I", "-c", LEGACY_GUARD_SDK_WORKER, "select", "billed_usd"],
        env={**env, "AGENTC_OPTIMIZE_PLAN_PROFILE_WINDOW": "25"},
        capture_output=True, text=True, timeout=120,
    )
    assert selected.returncode == 0, selected.stderr
    schedule = json.loads(selected.stdout)
    assert schedule["selected_reference"] and schedule["max_workers"] == 1, schedule
    assert schedule["reason"] == "no_admissible_alternative", schedule
    assert schedule["acquisition_cost_complete"]
    assert abs(schedule["completed_acquisition_cost_usd"] - 1.6) < 1e-9
    (tmp_path / "window-contract-selection.json").write_text(selected.stdout)
    audit_script = r"""
import json,sqlite3,sys
from pathlib import Path
assert not any(n=='agentc' or n.startswith('agentc.') for n in sys.modules)
conn=sqlite3.connect(Path(sys.argv[1])/'cost_model.db')
assert conn.execute('SELECT COUNT(*) FROM provider_receipt_invocation').fetchone()[0]==82
assert conn.execute('SELECT COUNT(*) FROM provider_original_execution').fetchone()[0]==41
assert conn.execute('SELECT COUNT(*) FROM provider_original_execution_pair').fetchone()[0]==20
assert conn.execute('SELECT COUNT(*) FROM provider_original_quality_event').fetchone()[0]==20
assert conn.execute('SELECT COUNT(*) FROM multi_call_comparison_pair').fetchone()[0]==20
assert conn.execute('SELECT SUM(n_paired_observations) FROM execution_plan_profile').fetchone()[0]==20
assert conn.execute('SELECT COUNT(*) FROM pragma_foreign_key_check').fetchone()[0]==0
assert sorted(row[0] for row in conn.execute('SELECT window_size FROM provider_original_execution_state'))==[25,50]
assert abs(conn.execute('SELECT SUM(total_cost_usd) FROM multi_call_comparison_spend').fetchone()[0]-1.6)<1e-9
"""
    audited = subprocess.run(
        [sys.executable, "-I", "-c", audit_script, str(tmp_path)],
        capture_output=True, text=True, timeout=30,
    )
    assert audited.returncode == 0, audited.stderr


@pytest.mark.parametrize("case", ["no_scope", "mixed_limit", "unselected_quantity", "invalid_integer"])
def test_tariff_policy_errors_stop_before_native_preparation_or_sdk_dispatch(tmp_path, case):
    script = r"""
import agentc,sys
agentc.init()
try:
    with agentc.application_version('invalid-tariff-policy-fixture'):
        def reject(policy):
            try:agentc.completion_batch([],site='invalid-tariff',policy=policy)
            except ValueError:return
            raise AssertionError('invalid economic policy was accepted')
        if sys.argv[1]=='no_scope':reject(agentc.BatchPolicy(100,economic_quantity='published_token_tariff_usd'))
        elif sys.argv[1]=='unselected_quantity':reject(agentc.BatchPolicy(100,max_expected_tariff_nanodollars=100))
        else:
            with agentc.tariff_accounting():
                if sys.argv[1]=='mixed_limit':reject(agentc.BatchPolicy(100,economic_quantity='published_token_tariff_usd',max_expected_cost_usd=1))
                else:reject(agentc.BatchPolicy(100,economic_quantity='published_token_tariff_usd',max_expected_tariff_nanodollars=True))
finally:agentc.shutdown()
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, case],
        env={**os.environ, "AGENTC_STORAGE_PATH": str(tmp_path)},
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
