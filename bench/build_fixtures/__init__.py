"""Fixture builders for the four reference agents.

Each submodule pulls from one public (or HF-gated) dataset and writes a
JSON file into ``bench/fixtures/<agent>.json`` in the shape that
:func:`bench.agents._runtime.load_tasks` expects:

    [
      {"task_id": "...", "prompt": "...", "expected": "...",
       "meta": {... dataset-specific extras ...}},
      ...
    ]

Run individually:

    python -m bench.build_fixtures.swebench
    python -m bench.build_fixtures.gaia
    python -m bench.build_fixtures.rag
    python -m bench.build_fixtures.multiagent

Or build all four (skipping any that fail, e.g. pending GAIA approval):

    python -m bench.build_fixtures
"""

from __future__ import annotations
