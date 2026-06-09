"""Guard threshold sweep — safety/savings frontier figure.

Runs run_guard_eval.py at multiple divergence-budget thresholds for two
(agent, config) pairs:
  - research_planner / ContextCompress-only  (benign target)
  - analyst_qa / StateDrop-only              (catastrophic target)

Env:
  GSWEEP_N              tasks per cell (default 100)
  GSWEEP_THRESHOLDS     comma list to override the default 7-point sweep
                        (useful for quick smoke tests, e.g. "0.10,0.20")

Outputs per cell:
  bench/paper_results/gsweep_rp_<threshold>.csv        (research_planner)
  bench/paper_results/gsweep_rp_<threshold>.per_task.csv
  bench/paper_results/gsweep_an_<threshold>.csv        (analyst_qa)
  bench/paper_results/gsweep_an_<threshold>.per_task.csv
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent

_THRESHOLDS = [0.05, 0.10, 0.15, 0.20, 0.30, 0.50, "off"]

_TARGETS = [
    {
        "short": "rp",
        "agent": "bench.agents.research_planner",
        "configs": "ContextCompress-only",
    },
    {
        "short": "an",
        "agent": "bench.agents.analyst_qa",
        "configs": "StateDrop-only",
    },
]

N = int(os.environ.get("GSWEEP_N", "100"))

_thresh_override = os.environ.get("GSWEEP_THRESHOLDS", "").strip()
if _thresh_override:
    _raw = [t.strip() for t in _thresh_override.split(",") if t.strip()]
    _THRESHOLDS = [float(t) if t != "off" else "off" for t in _raw]


def _run_cell(target: dict, threshold) -> None:
    tag_t = "off" if threshold == "off" else f"{float(threshold):.2f}"
    tag = f"gsweep_{target['short']}_{tag_t}"

    env = os.environ.copy()
    env["GE_AGENT"] = target["agent"]
    env["GE_CONFIGS"] = target["configs"]
    env["GE_N"] = str(N)
    env["GE_W"] = "0"
    env["GE_TAG"] = tag
    env["PYTHONPATH"] = str(_REPO / "python")

    if threshold == "off":
        env["AGENTC_OPTIMIZE_SHADOW"] = "0"
        env.pop("AGENTC_SHADOW_DIVERGENCE_BUDGET", None)
    else:
        env["AGENTC_OPTIMIZE_SHADOW"] = "1"
        env["AGENTC_SHADOW_DIVERGENCE_BUDGET"] = str(threshold)

    print(f"\n=== {tag}  threshold={threshold}  N={N} ===")
    proc = subprocess.run(
        [sys.executable, "-m", "bench.run_guard_eval"],
        cwd=str(_REPO),
        env=env,
        check=False,
    )
    if proc.returncode != 0:
        print(f"  FAILED (exit={proc.returncode})", file=sys.stderr)
        sys.exit(proc.returncode)


def main() -> int:
    for threshold in _THRESHOLDS:
        for target in _TARGETS:
            _run_cell(target, threshold)
    print("\nGuard sweep complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
