"""Real SDK acquisition accounting, independent of derived batch profiles."""

from __future__ import annotations

import json
import os
import subprocess
import sys


SDK_WORKER = r"""
import json,time
from dataclasses import asdict
from pathlib import Path
import agentc,httpx,openai
root=Path(__import__("os").environ["AGENTC_STORAGE_PATH"])
calls=[];fail=False
def handler(request):
    calls.append(json.loads(request.content));time.sleep(.01)
    if fail:
        return httpx.Response(500,json={"error":{"message":"fixture","type":"server_error"}})
    return httpx.Response(200,json={"id":"fixture","object":"chat.completion","created":0,"model":"gpt-4o-mini-2024-07-18","choices":[{"index":0,"finish_reason":"stop","message":{"role":"assistant","content":"answer"}}],"usage":{"prompt_tokens":2,"completion_tokens":1,"total_tokens":3,"cost":.01,"is_byok":False}})
client=openai.OpenAI(api_key="fixture",max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(handler)))
requests=[agentc.CompletionRequest("openai",{"model":"gpt-4o-mini-2024-07-18","messages":[{"role":"user","content":"PRIVATE_ECONOMIC_PROMPT_"+str(i)}],"max_tokens":8},client) for i in range(2)]
agentc.init()
def run(mode):
    observations=[];schedules=[]
    responses=agentc.completion_batch(requests,site="economics",policy=agentc.BatchPolicy(100000),max_workers=2,mode=mode,on_observation=observations.append,on_schedule=schedules.append)
    assert all(response.choices[0].message.content=="answer" for response in responses)
    return observations[0],schedules[0]
try:
    with agentc.application_version("economics-sdk-v1"):
        for i in range(20):
            reference,_=run("reference");candidate,_=run("candidate")
            pair=agentc.compare_parallel_observations(reference,candidate)
            assert pair.status=="recorded",pair
        # Completed SDK work remains charged when profile publication fails.
        original=agentc._native.optimize_observe_multi_call
        agentc._native.optimize_observe_multi_call=lambda _: '{"status":"abstained","reason":"injected publication failure"}'
        try:
            unprofiled,_=run("reference")
            assert unprofiled.status=="abstained",unprofiled
        finally:
            agentc._native.optimize_observe_multi_call=original
        _,before=run("guarded")
        assert before.max_workers==2,before
        assert before.acquisition_cost_complete is True,before
        assert abs(before.completed_acquisition_cost_usd-.82)<1e-12,before
        # A real SDK failure leaves its charge unknown, preserving the error.
        fail=True
        try:
            run("reference")
            raise AssertionError("failed SDK request unexpectedly succeeded")
        except openai.InternalServerError:
            pass
        fail=False
        _,after=run("guarded")
        assert after.max_workers==1 and after.reason=="acquisition_costs_incomplete",after
        assert after.acquisition_cost_complete is False and after.unknown_acquisition_invocations==1,after
        assert abs(after.completed_acquisition_cost_usd-.82)<1e-12,after
    assert len(calls)==87,len(calls)
    (root/"sdk-economic-proof.json").write_text(json.dumps({"http_calls":len(calls),"before":asdict(before),"after":asdict(after),"unprofiled_batch_id":unprofiled.provider_receipt_batch_id}))
finally:
    agentc.shutdown();client.close()
"""

RESTART_WORKER = r"""
import json
from dataclasses import asdict
from pathlib import Path
import agentc,httpx,openai
root=Path(__import__("os").environ["AGENTC_STORAGE_PATH"]);calls=[]
def handler(request):
    calls.append(1)
    return httpx.Response(200,json={"id":"fixture","object":"chat.completion","created":0,"model":"gpt-4o-mini-2024-07-18","choices":[{"index":0,"finish_reason":"stop","message":{"role":"assistant","content":"answer"}}],"usage":{"prompt_tokens":2,"completion_tokens":1,"total_tokens":3,"cost":.01,"is_byok":False}})
client=openai.OpenAI(api_key="fixture",max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(handler)))
requests=[agentc.CompletionRequest("openai",{"model":"gpt-4o-mini-2024-07-18","messages":[{"role":"user","content":"PRIVATE_ECONOMIC_PROMPT_"+str(i)}],"max_tokens":8},client) for i in range(2)]
agentc.init()
try:
    with agentc.application_version("economics-sdk-v1"):
        schedules=[]
        agentc.completion_batch(requests,site="economics",policy=agentc.BatchPolicy(100000),max_workers=2,on_schedule=schedules.append)
        schedule=schedules[0]
        assert schedule.max_workers==1 and schedule.reason=="acquisition_costs_incomplete",schedule
        assert schedule.unknown_acquisition_invocations==1
        assert abs(schedule.completed_acquisition_cost_usd-.82)<1e-12
        assert len(calls)==2
        proof=json.loads((root/"sdk-economic-proof.json").read_text());proof["restart_http_calls"]=2;proof["restart"]=asdict(schedule)
        (root/"sdk-economic-proof.json").write_text(json.dumps(proof))
finally:
    agentc.shutdown();client.close()
"""

SQL_WORKER = r"""
import json,sqlite3,sys
from pathlib import Path
root=Path(sys.argv[1]);assert not any(n=="agentc" or n.startswith("agentc.") for n in sys.modules)
proof=json.loads((root/"sdk-economic-proof.json").read_text())
conn=sqlite3.connect(root/"cost_model.db")
cohort=conn.execute("SELECT * FROM provider_economic_cohort").fetchall()
assert len(cohort)==1,cohort
receipts=[json.loads(row[0]) for row in conn.execute("SELECT completion_json FROM provider_receipt_invocation")]
assert len(receipts)==proof["http_calls"]+proof["restart_http_calls"]==89
assert sum(row["http_attempts"] for row in receipts)==89
assert sum(row["status"]=="failed" for row in receipts)==1
unknown=conn.execute("SELECT COUNT(*) FROM provider_economic_invocation WHERE unknown_cost=1 AND charged=1").fetchone()[0]
assert unknown==1
charged=conn.execute("SELECT SUM(known_cost_usd),COUNT(*) FROM provider_economic_invocation WHERE charged=1").fetchone()
assert abs(charged[0]-.82)<1e-12 and charged[1]==83,charged
legacy=conn.execute("SELECT SUM(total_cost_usd),SUM(completed_executions) FROM multi_call_comparison_spend").fetchone()
assert abs(legacy[0]-.8)<1e-12 and legacy[1]==40,legacy
covered=conn.execute("SELECT COUNT(*) FROM provider_economic_publication").fetchone()[0]
assert covered==40
unprofiled=proof["unprofiled_batch_id"]
assert conn.execute("SELECT COUNT(*) FROM provider_receipt_profile_publication WHERE batch_id=?",(unprofiled,)).fetchone()[0]==0
assert conn.execute("SELECT SUM(known_cost_usd) FROM provider_economic_invocation WHERE batch_id=?",(unprofiled,)).fetchone()[0]==.02
for table in ["provider_economic_cohort","provider_economic_batch","provider_economic_branch","provider_economic_invocation","provider_economic_publication"]:
    for row in conn.execute("SELECT * FROM "+table):
        assert all(marker not in str(row) for marker in ["PRIVATE_ECONOMIC_PROMPT","api_key","authorization"])
snapshot={"sdk_imported":False,"physical_http_sends":89,"charged_invocations":charged[1],"known_fixture_acquisition_cost_usd":charged[0],"unknown_invocations":unknown,"covered_profile_executions":covered,"legacy_fixture_cost_usd":legacy[0],"cohort":cohort}
(root/"economic-sql-audit.json").write_text(json.dumps(snapshot))
"""


def test_real_sdk_unprofiled_work_is_charged_and_unknown_failure_blocks_schedule(
    tmp_path,
):
    env = {
        **os.environ,
        "AGENTC_STORAGE_PATH": str(tmp_path),
        "AGENTC_OPTIMIZE": "1",
        "AGENTC_OPTIMIZE_EXPLORATION": "0",
        "AGENTC_OPTIMIZE_SHADOW": "0",
        "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": "1000",
        "AGENTC_CAPTURE_CONTENT": "0",
        "AGENTC_CAPTURE_EMBEDDINGS": "0",
    }
    for script, args in [
        (SDK_WORKER, []),
        (RESTART_WORKER, []),
        (SQL_WORKER, [str(tmp_path)]),
    ]:
        result = subprocess.run(
            [sys.executable, "-I", "-c", script, *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
        )
        assert result.returncode == 0, result.stderr
    audit = json.loads((tmp_path / "economic-sql-audit.json").read_text())
    assert audit["physical_http_sends"] == 89 and audit["unknown_invocations"] == 1


def test_calibration_without_durable_enrollment_dispatches_nothing(monkeypatch):
    import agentc
    import httpx
    import openai

    calls = []
    client = openai.OpenAI(
        api_key="fixture",
        max_retries=0,
        http_client=httpx.Client(
            transport=httpx.MockTransport(lambda request: calls.append(request))
        ),
    )
    monkeypatch.setattr(agentc, "is_initialized", lambda: True)
    monkeypatch.setattr(
        agentc._native,
        "optimize_prepare_provider_batch",
        lambda *_: '{"status":"abstained","reason":"storage unavailable"}',
    )
    try:
        with agentc.application_version("failed-enrollment-v1"):
            request = agentc.CompletionRequest(
                "openai",
                {
                    "model": "gpt-4o-mini",
                    "messages": [{"role": "user", "content": "a"}],
                },
                client,
            )
            import pytest

            for mode in ["reference", "candidate", "guarded"]:
                with pytest.raises(RuntimeError, match="durable provider accounting"):
                    agentc.completion_batch(
                        [request],
                        site="missing-owner",
                        policy=agentc.BatchPolicy(1),
                        mode=mode,
                    )
        assert calls == []
    finally:
        client.close()
