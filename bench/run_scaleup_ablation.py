"""Scale-up ablation: 5-config targeted sweep for autogen_bridge and rag_summarizer.

Runs only the configs that matter for the paper narrative:
  - all-on   : full optimizer
  - all-off  : optimizer active but all rules disabled (pure overhead baseline)
  - CC-off   : ContextCompress disabled
  - SD-off   : StateDrop disabled
  - OB-off   : OutputBudget disabled

Designed to run on Together AI (Llama-3.3-70B-Instruct-Turbo) via
BENCH_OPENAI_BASE_URL + TOGETHER_API_KEY, with a hard spend guard.

Usage:
    python -m bench.run_scaleup_ablation
    python -m bench.run_scaleup_ablation --n-tasks 20  # smoke test
    python -m bench.run_scaleup_ablation --agents bench.agents.autogen_bridge
"""

from __future__ import annotations

import argparse
import csv
import os
import shutil
import sys
from pathlib import Path
from typing import Optional

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "python"))

from bench.optimizer_ablation import (
    RULES,
    AblationRow,
    _CSV_COLUMNS,
    _run_config,
    append_row,
    write_header,
)
from bench.optimizer_bench import _find_agentc_binary

PAPER_RESULTS = _REPO / "bench" / "paper_results"
STORAGE_ROOT = Path("/tmp/agentc-scaleup-ablation")

_TOGETHER_BASE_URL = "https://api.together.xyz/v1"
_TOGETHER_MODEL = "meta-llama/Llama-3.3-70B-Instruct-Turbo"

# 5-config targeted sweep: (config_name, rules_to_disable)
_CONFIGS: list[tuple[str, list[str]]] = [
    ("all-on",  []),
    ("all-off", list(RULES)),
    ("ContextCompress-off", ["ContextCompress"]),
    ("StateDrop-off",       ["StateDrop"]),
    ("OutputBudget-off",    ["OutputBudget"]),
]

_DEFAULT_AGENTS = [
    "bench.agents.autogen_bridge",
    "bench.agents.rag_summarizer",
]

_HARD_STOP_USD = 75.0


def _load_env(repo: Path) -> dict[str, str]:
    env = os.environ.copy()
    env_file = repo / ".env"
    if env_file.is_file():
        for raw in env_file.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in env:
                env[k] = v
    return env


def _together_env(base: dict[str, str]) -> dict[str, str]:
    """Return extra_env dict for Together AI routing."""
    together_key = base.get("TOGETHER_API_KEY", "")
    if not together_key:
        raise RuntimeError("TOGETHER_API_KEY not set — add it to .env or environment")
    return {
        "BENCH_OPENAI_BASE_URL": _TOGETHER_BASE_URL,
        "BENCH_BASELINE_MODEL": _TOGETHER_MODEL,
        "TOGETHER_API_KEY": together_key,
        # Pass TOGETHER_API_KEY through so _runtime.py picks it up
        # (URL-aware key selection added in bench/agents/_runtime.py)
    }


def _estimate_spend_usd(out_path: Path) -> float:
    """Sum optimized_cost_usd from a CSV written by this script."""
    if not out_path.exists():
        return 0.0
    total = 0.0
    with out_path.open() as f:
        for row in csv.DictReader(f):
            try:
                v = float(row.get("optimized_cost_usd", 0) or 0)
                if v < 1.0:  # sanity: ignore stub $0 rows only if clearly bad
                    total += v
            except ValueError:
                pass
    return total


def _global_spend_usd(out_paths: list[Path]) -> float:
    return sum(_estimate_spend_usd(p) for p in out_paths)


def run_scaleup(
    agents: list[str],
    n_tasks: int,
    storage_root: Path,
    out_paths: dict[str, Path],
    extra_env: dict[str, str],
    hard_stop_usd: float = _HARD_STOP_USD,
) -> None:
    all_out_paths = list(out_paths.values())

    for agent_module in agents:
        out_path = out_paths[agent_module]
        write_header(out_path)
        agent_root = storage_root / agent_module.replace(".", "_")
        if agent_root.exists():
            shutil.rmtree(agent_root)
        agent_root.mkdir(parents=True)

        agent_env = dict(extra_env)
        agent_env["BENCH_MAX_TASKS"] = str(n_tasks)

        print(f"\n{'='*70}")
        print(f"AGENT: {agent_module}  (n={n_tasks})")
        print(f"{'='*70}")

        for config_name, rules_off in _CONFIGS:
            spend = _global_spend_usd(all_out_paths)
            if spend >= hard_stop_usd:
                print(f"\nHARD STOP: ${spend:.2f} >= ${hard_stop_usd:.2f}")
                sys.exit(1)

            print(f"\n--- [{config_name}] ---  (cumulative spend ${spend:.2f})")
            row = _run_config(
                agent_module=agent_module,
                config=config_name,
                rules_off=rules_off,
                storage_root=agent_root,
                extra_env=agent_env,
            )
            append_row(row, out_path)
            print(f"  → wrote to {out_path}")

        print(f"\nAgent {agent_module} done. Output: {out_path}")


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m bench.run_scaleup_ablation",
        description="5-config targeted ablation for autogen_bridge and rag_summarizer.",
    )
    p.add_argument(
        "--agents", nargs="+", default=_DEFAULT_AGENTS,
        help="Agent module paths (default: autogen_bridge rag_summarizer)",
    )
    p.add_argument(
        "--n-tasks", type=int, default=200,
        help="Tasks per condition (default: 200)",
    )
    p.add_argument(
        "--storage-root", type=Path, default=STORAGE_ROOT,
        help=f"Storage root for isolated agentc DBs (default: {STORAGE_ROOT})",
    )
    p.add_argument(
        "--hard-stop-usd", type=float, default=_HARD_STOP_USD,
        help=f"Abort if cumulative spend exceeds this (default: ${_HARD_STOP_USD})",
    )
    args = p.parse_args(argv)

    env = _load_env(_REPO)
    extra_env = _together_env(env)

    PAPER_RESULTS.mkdir(parents=True, exist_ok=True)

    out_paths: dict[str, Path] = {}
    for agent in args.agents:
        short = agent.split(".")[-1]  # e.g. "autogen_bridge"
        out_paths[agent] = PAPER_RESULTS / f"{short}_n{args.n_tasks}_isolated.csv"

    print("Together AI scaleup ablation")
    print(f"  model : {_TOGETHER_MODEL}")
    print(f"  agents: {args.agents}")
    print(f"  n     : {args.n_tasks}")
    print(f"  configs: {[c for c, _ in _CONFIGS]}")
    print(f"  outputs: {list(out_paths.values())}")
    print(f"  hard stop: ${args.hard_stop_usd:.2f}")

    run_scaleup(
        agents=args.agents,
        n_tasks=args.n_tasks,
        storage_root=args.storage_root,
        out_paths=out_paths,
        extra_env=extra_env,
        hard_stop_usd=args.hard_stop_usd,
    )

    print("\nAll agents complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
