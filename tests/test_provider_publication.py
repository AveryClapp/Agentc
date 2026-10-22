"""Recovery across abrupt SDK-process exits, audited without the extension."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


SDK_WORKER = r"""
import json,os,sys
from pathlib import Path
import agentc,httpx,openai
mode=sys.argv[1];root=Path(os.environ["AGENTC_STORAGE_PATH"])
agentc.init()
if mode=="initialize":
    agentc.shutdown();sys.exit(0)
if mode=="recover":
    raw=(root/"receipt.json").read_text()
    result=json.loads(agentc._native.optimize_observe_multi_call(raw))
    assert result["status"]=="recorded",result
    edited=json.loads(raw);edited["wall_latency_ms"]+=1
    assert json.loads(agentc._native.optimize_observe_multi_call(json.dumps(edited)))["status"]=="abstained"
    (root/"recovered.json").write_text(json.dumps(result))
    agentc.shutdown();sys.exit(0)
http_calls=[]
def handler(request):
    body=json.loads(request.content);http_calls.append(body)
    return httpx.Response(200,json={"id":"fixture","object":"chat.completion","created":0,"model":"gpt-4o-mini-2024-07-18","choices":[{"index":0,"finish_reason":"stop","message":{"role":"assistant","content":"answer"}}],"usage":{"prompt_tokens":2,"completion_tokens":1,"total_tokens":3,"cost":0.01,"is_byok":False}})
client=openai.OpenAI(api_key="fixture",max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(handler)))
observe=agentc._native.optimize_observe_multi_call
def capture(value):
    (root/"receipt.json").write_text(value)
    result=observe(value)
    (root/"initial-result.json").write_text(result)
    return result
agentc._native.optimize_observe_multi_call=capture
with agentc.application_version("crash-recovery-v1"):
    output=[]
    requests=[agentc.CompletionRequest("openai",{"model":"gpt-4o-mini","messages":[{"role":"user","content":"PRIVATE_CRASH_RECOVERY_PROMPT_"+str(i)}],"max_tokens":8},client) for i in range(2)]
    responses=agentc.completion_batch(requests,site="publication-crash",policy=agentc.BatchPolicy(1000),mode="reference",on_observation=output.append)
    assert len(responses)==2 and all(response.choices[0].message.content=="answer" for response in responses)
    assert len(http_calls)==2
    (root/"worker-proof.json").write_text(json.dumps({"http_calls":len(http_calls),"batch_id":output[0].provider_receipt_batch_id,"status":output[0].status}))
# Skip SDK lifecycle flushing and destructors to reproduce process loss.
os._exit(0)
"""

SQL_WORKER = r"""
import json,sqlite3,sys
from pathlib import Path
root=Path(sys.argv[1]);operation=sys.argv[2]
assert not any(name=="agentc" or name.startswith("agentc.") for name in sys.modules)
conn=sqlite3.connect(root/"cost_model.db")
if operation=="inject":
    table=sys.argv[3]
    if table!="committed":
        action="UPDATE" if table=="provider_receipt_profile_publication" else "INSERT"
        condition="WHEN NEW.provider_protocol='agentc.multi-call-runtime.v1'" if table in ("execution_plan_profile","execution_plan_observation") else ""
        conn.executescript(f"CREATE TRIGGER interrupt_publication BEFORE {action} ON {table} {condition} BEGIN SELECT RAISE(ABORT,'injected storage failure'); END;")
elif operation=="clear":
    conn.execute("DROP TRIGGER IF EXISTS interrupt_publication")
else:
    proof=json.loads((root/"worker-proof.json").read_text())
    batch_id=proof["batch_id"]
    journal=conn.execute("SELECT invocation_json,completion_json FROM provider_receipt_invocation WHERE batch_id=? ORDER BY item",(batch_id,)).fetchall()
    assert len(journal)==2
    completions=[json.loads(row[1]) for row in journal]
    assert sum(row["http_attempts"] for row in completions)==proof["http_calls"]==2
    assert sum(row["cost_usd"] for row in completions)==0.02
    published=conn.execute("SELECT result_json FROM provider_receipt_profile_publication WHERE batch_id=?",(batch_id,)).fetchone()[0]
    recovery_input=conn.execute("SELECT input_json FROM provider_receipt_publication_input WHERE batch_id=?",(batch_id,)).fetchone()[0]
    for text in [recovery_input]+[value for row in journal for value in row]:
        assert "PRIVATE_CRASH_RECOVERY_PROMPT" not in text and "api_key" not in text and "authorization" not in text
    profiles=conn.execute("SELECT n_observations FROM execution_plan_profile WHERE provider_protocol='agentc.multi-call-runtime.v1'").fetchall()
    spend=conn.execute("SELECT completed_executions,total_cost_usd FROM multi_call_comparison_spend").fetchall()
    tokens=conn.execute("SELECT token FROM multi_call_comparison_execution").fetchall()
    snapshot={"profiles":profiles,"spend":spend,"tokens":tokens,"result":published,"http_calls":2}
    (root/(operation+"-audit.json")).write_text(json.dumps(snapshot))
conn.commit();conn.close()
"""


@pytest.mark.parametrize(
    "boundary",
    [
        "execution_plan_profile",
        "execution_plan_observation",
        "multi_call_comparison_spend",
        "multi_call_comparison_execution",
        "provider_receipt_profile_publication",
        "committed",
    ],
)
def test_sdk_process_loss_recovers_exactly_once_without_provider_replay(
    tmp_path: Path, boundary: str
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

    def run(script: str, *args: str):
        process = subprocess.run(
            [sys.executable, "-I", "-c", script, *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert process.returncode == 0, process.stderr

    run(SDK_WORKER, "initialize")
    run(SQL_WORKER, str(tmp_path), "inject", boundary)
    run(SDK_WORKER, "execute")
    run(SQL_WORKER, str(tmp_path), "before")
    before = json.loads((tmp_path / "before-audit.json").read_text())
    if boundary == "committed":
        assert before["profiles"] == [[1]]
        assert before["spend"] == [[1, 0.02]]
        assert len(before["tokens"]) == 1 and before["result"] is not None
    else:
        assert before["profiles"] == before["spend"] == before["tokens"] == []
        assert before["result"] is None
    run(SQL_WORKER, str(tmp_path), "clear")
    run(SDK_WORKER, "recover")
    run(SQL_WORKER, str(tmp_path), "after")
    after = json.loads((tmp_path / "after-audit.json").read_text())
    assert after["profiles"] == [[1]] and after["spend"] == [[1, 0.02]]
    assert len(after["tokens"]) == 1
    assert json.loads(after["result"])["status"] == "recorded"
    run(SDK_WORKER, "recover")
    run(SQL_WORKER, str(tmp_path), "retry")
    assert json.loads((tmp_path / "retry-audit.json").read_text()) == after
