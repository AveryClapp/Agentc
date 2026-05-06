"""Agentc benchmark suite.

Two independent harnesses live here:

- ``optimizer_bench`` / ``optimizer_ablation`` — end-to-end runs of the
  reference agents under ``bench/agents/`` with the optimizer toggled
  on and off, plus a per-rule ablation sweep. Reads materialized task
  fixtures from ``bench/fixtures/`` (built by the top-level
  ``build_{gaia,hotpot,long_context}_fixture.py`` scripts). Driver
  shell scripts live under ``bench/scripts/``.
- ``harness`` / ``calibrate`` / ``overhead`` / ``validate`` /
  ``report`` / ``run`` — profiler-overhead and waste-detector
  calibration suite over synthetic mock pipelines. Run with
  ``python -m bench.run`` or ``pytest bench/test_benchmark.py``.
"""
