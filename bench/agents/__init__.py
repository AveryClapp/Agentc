"""Reference agents for the optimizer ship gate (O9).

Each agent in this package is a self-contained callable that:

- Accepts a ``task_id`` and returns an ``AgentResult`` with ``answer``
  and a per-task pass/fail check.
- Reads its tasks from a JSON fixture file (under ``bench/fixtures/``)
  when present; falls back to the small hand-authored fixtures in
  :mod:`bench.agents._fixtures` so the optimizer harness is runnable
  end-to-end without external datasets.
- Uses ``agentc.record`` to wrap its top-level call so the optimizer
  interception path can observe it.

The four agents correspond to the reference workloads called out in
``specs/optimizer.md`` → Evaluation: SWE-bench planner, GAIA router,
RAG summarizer, multi-agent research.
"""

from __future__ import annotations
