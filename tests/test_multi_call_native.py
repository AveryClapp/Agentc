"""Real SDK adapters, ordered map collection, native storage and offline audit."""

from __future__ import annotations

import json
import os
import subprocess
import sys


def test_heterogeneous_map_profiles_billed_calls_and_abstains_on_incomplete_work(
    tmp_path,
):
    script = r"""
import json, threading, time
from dataclasses import asdict
from types import SimpleNamespace
import agentc
from agentc._patches._openai import _wrap_create
from agentc._patches._litellm import _wrap_completion

calls, observations = [], []
lock = threading.Lock()
reported = True
revision = None
skip_receipt = False
items = ["first-private-prompt", "second-private-prompt", "third-private-prompt"]

def fn(item):
    i = items.index(item)
    model = "gpt-4o-mini" if i != 1 else "openai/gpt-4o"
    def wrapped(**kwargs):
        time.sleep(0.003 * (3-i))
        with lock:
            calls.append((i, kwargs["model"]))
        usage = SimpleNamespace(prompt_tokens=7+i, completion_tokens=3+i)
        if reported:
            usage.cost, usage.is_byok = 0.01 * (i+1), False
        return SimpleNamespace(model=revision if i == 1 and revision else kwargs["model"],
            usage=usage, choices=[SimpleNamespace(message=SimpleNamespace(content="answer"))])
    kwargs = {"model":model,"messages":[{"role":"user","content":item}], "max_tokens":16}
    wrapper = _wrap_completion if i == 1 else _wrap_create
    result = wrapper(wrapped, None, (), kwargs)
    if skip_receipt and i == 1:
        # A real trace-only SDK invocation alongside observed calls must not
        # disappear from the batch's accounting.
        with agentc.optimization_scope("unprofiled", optimize=False):
            wrapper(wrapped, None, (), kwargs)
    return result

agentc.init()
try:
    with agentc.application_version("batch-release-v1"), agentc.optimization_scope("fixture", optimize=True):
        for workers in [3, 3, 1]:
            results = agentc.parallel_map(fn, items, max_workers=workers, on_observation=observations.append)
            assert len(results) == 3
            assert all(result.choices[0].message.content == "answer" for result in results)
        reported = False
        agentc.parallel_map(fn, items, on_observation=observations.append)
        reported = True
        skip_receipt = True
        agentc.parallel_map(fn, items, on_observation=observations.append)
        skip_receipt = False
        revision = "openai/gpt-4o-new-provider-revision"
        agentc.parallel_map(fn, items, on_observation=observations.append)
    with agentc.application_version("batch-release-v2"), agentc.optimization_scope("fixture", optimize=True):
        agentc.parallel_map(fn, items, on_observation=observations.append)
    print(json.dumps({"observations":[asdict(value) for value in observations], "physical_calls":len(calls)}))
finally:
    agentc.shutdown()
"""
    proc = subprocess.run(
        [sys.executable, "-c", script],
        env={
            **os.environ,
            "AGENTC_STORAGE_PATH": str(tmp_path),
            "AGENTC_OPTIMIZE": "1",
            "AGENTC_OPTIMIZE_EXPLORATION": "0",
            "AGENTC_OPTIMIZE_SHADOW": "0",
            "AGENTC_ENABLED_RULES": "ParallelBranch",
            "AGENTC_CAPTURE_CONTENT": "0",
            "AGENTC_CAPTURE_EMBEDDINGS": "0",
            "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": "1000",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    observations = result["observations"]
    assert result["physical_calls"] == 22
    assert [value["status"] for value in observations] == [
        "recorded",
        "recorded",
        "recorded",
        "abstained",
        "abstained",
        "recorded",
        "recorded",
    ]
    for index in [0, 1, 2, 5, 6]:
        value = observations[index]
        assert value["sdk_calls"] == value["observed_calls"] == 3
        assert abs(value["cost_usd"] - 0.06) < 1e-12
        assert value["input_tokens"] == 24
        assert value["output_tokens"] == 12
    assert observations[0]["execution_plan_id"] == observations[1]["execution_plan_id"]
    assert observations[0]["execution_plan_id"] != observations[2]["execution_plan_id"]
    assert observations[0]["call_site_version"] == observations[2]["call_site_version"]
    assert observations[0]["execution_plan_id"] == observations[5]["execution_plan_id"]
    assert observations[0]["call_site_version"] != observations[6]["call_site_version"]
    assert observations[3]["cost_usd"] is None
    assert observations[4]["reason"] == "unprofiled_sdk_call"
    assert observations[4]["sdk_calls"] == 4

    # The SDK process has exited before this SDK-free SQLite audit begins.
    audit = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            """
import json, sqlite3, sys
with sqlite3.connect(sys.argv[1]) as conn:
    rows = conn.execute(
        "SELECT call_site_version, execution_plan_id, n_observations, window_observations, "
        "paired_observations, target_model_version, cost_usd_mean FROM execution_plan_profile "
        "WHERE provider_protocol = 'agentc.multi-call-runtime.v1'"
    ).fetchall()
assert 'agentc' not in sys.modules
print(json.dumps(rows))
""",
            str(tmp_path / "cost_model.db"),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    rows = json.loads(audit.stdout)
    assert len(rows) == 3
    assert sum(row[2] for row in rows) == 5
    assert all(row[4] == 0 for row in rows)
    assert all(abs(row[6] - 0.06) < 1e-12 for row in rows)
    parallel = next(
        row
        for row in rows
        if row[0] == observations[0]["call_site_version"]
        and row[1] == observations[0]["execution_plan_id"]
    )
    assert parallel[2:4] == [3, 1]  # only the new second-branch revision is retained
    versions = json.loads(parallel[5])
    assert len(versions) == 3
    assert [value["runtime"]["provider_protocol"] for value in versions] == [
        "openai.chat.completions.v1",
        "litellm.completion.v1",
        "openai.chat.completions.v1",
    ]
    assert (
        versions[1]["runtime"]["target_model_version"]
        == "openai/gpt-4o-new-provider-revision"
    )
    assert "private-prompt" not in json.dumps(rows)


def test_real_completed_maps_compare_once_recover_after_restart_and_charge_both_runs(
    tmp_path,
):
    script = r"""
import json, threading
from dataclasses import asdict
from types import SimpleNamespace
import agentc
from agentc._patches._openai import _wrap_create
calls, observations, comparisons = [], [], []
from agentc._patches._litellm import _wrap_completion
lock = threading.Lock()
different = False
opaque = False
items = ["private-first", "private-second", "private-third"]

def fn(item):
    def wrapped(**kwargs):
        with lock:
            calls.append(kwargs["messages"][0]["content"])
        answer = "changed" if different and item == items[1] else "answer"
        return SimpleNamespace(model=kwargs["model"], usage=SimpleNamespace(prompt_tokens=2, completion_tokens=1, cost=0.01, is_byok=False), choices=[SimpleNamespace(message=SimpleNamespace(content=answer))])
    wrapper = _wrap_completion if item == items[1] else _wrap_create
    model = "openai/gpt-4o" if item == items[1] else "gpt-4o-mini"
    response = wrapper(wrapped, None, (), {"model":model,"messages":[{"role":"user","content":item}],"max_tokens":16})
    return object() if opaque else {"item":item,"answer":response.choices[0].message.content}

def run(workers, values=items):
    result = agentc.parallel_map(fn, values, max_workers=workers, comparison=True, on_observation=observations.append)
    assert len(result)==3
    assert observations[-1].status=="recorded", observations[-1]
    return observations[-1]

agentc.init()
try:
    with agentc.application_version("comparison-release-v1"), agentc.optimization_scope("fixture", optimize=True):
        reference, candidate = run(1), run(3)
        assert reference.comparison_token and candidate.comparison_token, observations
        before = len(calls)
        comparisons.append(agentc.compare_parallel_observations(reference,candidate))
        comparisons.append(agentc.compare_parallel_observations(reference,candidate))
        assert len(calls)==before==6
        borrowed = run(3)
        comparisons.append(agentc.compare_parallel_observations(reference,borrowed))
        other_reference, other_candidate = run(1), run(3,list(reversed(items)))
        comparisons.append(agentc.compare_parallel_observations(other_reference,other_candidate))
        bad_reference = run(1)
        different = True
        bad_candidate = run(3)
        comparisons.append(agentc.compare_parallel_observations(bad_reference,bad_candidate))
        opaque = True
        unsupported = run(3)
        assert unsupported.comparison_token is None
        assert unsupported.reason=="comparison_results_unsupported"
    agentc.shutdown()
    agentc.init()
    before = len(calls)
    comparisons.append(agentc.compare_parallel_observations(reference,candidate))
    assert len(calls)==before==24
    print(json.dumps({"observations":[asdict(value) for value in observations],"comparisons":[asdict(value) for value in comparisons],"local_fixture_calls":len(calls)}))
finally:
    agentc.shutdown()
"""
    proc = subprocess.run(
        [sys.executable, "-c", script],
        env={
            **os.environ,
            "AGENTC_STORAGE_PATH": str(tmp_path),
            "AGENTC_OPTIMIZE": "1",
            "AGENTC_OPTIMIZE_EXPLORATION": "0",
            "AGENTC_OPTIMIZE_SHADOW": "0",
            "AGENTC_ENABLED_RULES": "ParallelBranch",
            "AGENTC_CAPTURE_CONTENT": "0",
            "AGENTC_CAPTURE_EMBEDDINGS": "0",
            "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": "1000",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["local_fixture_calls"] == 24
    comparisons = result["comparisons"]
    assert [value["status"] for value in comparisons] == [
        "recorded",
        "already_recorded",
        "abstained",
        "abstained",
        "recorded",
        "already_recorded",
    ]
    assert comparisons[0]["divergence"] == 0.0
    assert comparisons[4]["divergence"] == 1.0
    assert (
        comparisons[0]["pair_id"]
        == comparisons[1]["pair_id"]
        == comparisons[5]["pair_id"]
    )
    assert all(
        abs(comparisons[index]["acquisition_cost_usd"] - 0.06) < 1e-12
        for index in [0, 1, 4, 5]
    )
    audit = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            """
import json,sqlite3,sys
with sqlite3.connect(sys.argv[1]) as conn:
    pairs=conn.execute('SELECT divergence,acquisition_cost_usd FROM multi_call_comparison_pair ORDER BY rowid').fetchall()
    acquisition=conn.execute('SELECT completed_pairs,total_cost_usd FROM multi_call_comparison_acquisition').fetchall()
    spend=conn.execute('SELECT SUM(completed_executions),SUM(total_cost_usd) FROM multi_call_comparison_spend').fetchall()
    profiles=conn.execute("SELECT paired_observations,divergence_upper_p95 FROM execution_plan_profile WHERE provider_protocol='agentc.multi-call-runtime.v1' AND paired_observations>0").fetchall()
    bindings=conn.execute('SELECT binding_json FROM multi_call_comparison_execution').fetchall()
assert 'agentc' not in sys.modules
print(json.dumps({'pairs':pairs,'acquisition':acquisition,'spend':spend,'profiles':profiles,'bindings':bindings}))
""",
            str(tmp_path / "cost_model.db"),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    data = json.loads(audit.stdout)
    assert data["pairs"] == [[0.0, 0.06], [1.0, 0.06]]
    assert data["acquisition"] == [[2, 0.12]]
    assert data["spend"][0][0] == 8
    assert abs(data["spend"][0][1] - 0.24) < 1e-12
    assert data["profiles"] == [[2, 1.0]]
    assert "private-first" not in json.dumps(data["bindings"])
    assert "private-second" not in json.dumps(data["bindings"])
    binding = json.loads(data["bindings"][0][0])
    versions = json.loads(
        binding["observation"]["runtime_version"]["target_model_version"]
    )
    assert [branch["runtime"]["provider_protocol"] for branch in versions] == [
        "openai.chat.completions.v1",
        "litellm.completion.v1",
        "openai.chat.completions.v1",
    ]
