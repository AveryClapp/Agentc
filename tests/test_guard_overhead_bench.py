"""Verify measured fresh feedback reaches the durable guard exactly once."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("workers,sites", [(1, 1), (4, 4)])
def test_feedback_benchmark_verifies_concurrent_durability_without_loading_sqlite(workers, sites):
    worker = r"""
import agentc,json,sys
sys.path.insert(0,sys.argv[1])
from bench.guard_overhead_bench import run
assert 'sqlite3' not in sys.modules
workers,sites=map(int,sys.argv[2:])
p=run(fresh_tokens=24,metric_iters=20,warmup_tokens=2,workers=workers,sites=sites)
assert 'sqlite3' not in sys.modules
assert p['validated_state']['plan_guard_rows']==sites
assert p['validated_state']['lifetime_paired_observations']==24+sites*2
assert p['validated_state']['retained_paired_observations']==24+sites*2
assert p['settings']['concurrent_feedback_workers']==workers
assert p['settings']['call_sites']==sites and p['settings']['observed_feedback_fraction']==1.
f=p['measurements_us']['fresh_complete_plan_feedback']
assert 0<f['p50_us']<=f['p95_us']<=f['p99_us']
assert f['feedback_per_second']>0 and f['wall_seconds']>0
assert not p['paper_evidence'] and p['network_calls']==0
assert p['durability_verified_before_shutdown']
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", worker, str(Path(__file__).resolve().parents[1]),
         str(workers), str(sites)],
        env={**os.environ, "AGENTC_CAPTURE_CONTENT": "0", "AGENTC_CAPTURE_EMBEDDINGS": "0"},
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr


def test_feedback_benchmark_rejects_missing_durable_observations(tmp_path):
    audit = r"""
import sqlite3,sys
with sqlite3.connect(sys.argv[1]) as c:
    c.executescript('CREATE TABLE execution_plan_guard(call_site_version TEXT,execution_plan_id TEXT,divergence_threshold REAL,divergence_exposure REAL,window_samples INTEGER); CREATE TABLE execution_plan_profile(call_site_version TEXT,execution_plan_id TEXT,n_paired_observations INTEGER,paired_observations INTEGER); CREATE TABLE rule_divergence(call_site_id TEXT);')
    c.execute('INSERT INTO execution_plan_guard VALUES(?,?,?,?,?)',('site','plan',.01,0.,0))
    c.execute('INSERT INTO execution_plan_profile VALUES(?,?,?,?)',('site','plan',2,2))
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", audit, str(tmp_path / "cost_model.db")],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    worker = r"""
import agentc,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from bench.guard_overhead_bench import _verify_complete_plan_guard
assert 'sqlite3' not in sys.modules
try:_verify_complete_plan_guard(Path(sys.argv[2]),expected_paired_observations=[3])
except RuntimeError as exc:assert 'guard audit failed' in str(exc)
else:raise AssertionError('benchmark accepted a missing durable feedback sample')
assert 'sqlite3' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", worker, str(Path(__file__).resolve().parents[1]), str(tmp_path)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
