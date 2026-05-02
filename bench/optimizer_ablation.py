"""Per-rule ablation sweep over the five optimizer rewrite rules.

For each reference agent, runs ``optimizer_bench`` under the following
configurations:

- ``all-on``: nothing disabled — the reference point.
- ``<rule>-off``: every rule enabled *except* one — how much does
  removing just this rule cost us?
- ``<rule>-only``: only this rule enabled — how much does this rule
  carry on its own?

Writes a CSV contribution matrix: one row per (agent, configuration)
with savings% / accuracy delta.

Per ``specs/optimizer.md`` ship gate: the ``<rule>-off`` column for at
least one agent must show materially lower savings than ``all-on`` —
i.e. the rules are not redundant.

Implementation: we disable rules by shelling out to
``agentc optimize disable --rule <name> --call-site '*' --hours 9999``
against a seeded cost-model DB before each run. We use a fresh storage
root per configuration so prior disables don't leak across runs.
"""

from __future__ import annotations

import argparse
import csv
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from bench.optimizer_bench import (
    BenchResult,
    _find_agentc_binary,
    render_result,
    run_bench,
)


RULES: list[str] = [
    "CacheHit",
    "ContextCompress",
    "ParallelBranch",
    "ModelDowngrade",
    "StateDrop",
]


@dataclass
class AblationRow:
    agent_module: str
    config: str  # "all-on" | "<rule>-off" | "<rule>-only"
    cost_savings_pct: float
    accuracy_delta_pp: float
    baseline_cost_usd: float
    optimized_cost_usd: float
    n_tasks: int
    n_passed_optimized: int

    @classmethod
    def from_result(cls, config: str, result: BenchResult) -> "AblationRow":
        return cls(
            agent_module=result.agent_module,
            config=config,
            cost_savings_pct=result.cost_savings_pct,
            accuracy_delta_pp=result.accuracy_delta_pct,
            baseline_cost_usd=result.baseline.total_cost_usd,
            optimized_cost_usd=result.optimized.total_cost_usd,
            n_tasks=result.baseline.n_tasks,
            n_passed_optimized=result.optimized.n_passed,
        )


def _disable(rules_to_disable: list[str], storage_dir: Path) -> None:
    """Seed the optimized storage dir by pre-populating the cost model
    with rule-disable entries. We call the ``agentc optimize disable``
    subcommand directly — that's the public knob for this (O8)."""
    storage_dir.mkdir(parents=True, exist_ok=True)
    if not rules_to_disable:
        return
    agentc_bin = _find_agentc_binary()
    for rule in rules_to_disable:
        subprocess.run(
            [
                agentc_bin,
                "optimize",
                "disable",
                "--rule",
                rule,
                "--call-site",
                "*",
                "--hours",
                "9999",
                "--reason",
                "ablation",
                "--storage-path",
                str(storage_dir),
            ],
            check=True,
            capture_output=True,
        )


def _run_config(
    *,
    agent_module: str,
    config: str,
    rules_off: list[str],
    storage_root: Path,
) -> AblationRow:
    sub_root = storage_root / config
    if sub_root.exists():
        shutil.rmtree(sub_root)
    sub_root.mkdir(parents=True)
    # ``disable`` writes to the *optimized* side only — baseline runs with
    # AGENTC_OPTIMIZE=0 so disables are irrelevant there.
    _disable(rules_off, sub_root / "optimized")
    result = run_bench(
        agent_module=agent_module,
        storage_root=sub_root,
        rules_disabled=rules_off,
    )
    print(f"\n=== {config} ({agent_module}) ===")
    print(render_result(result))
    return AblationRow.from_result(config, result)


def sweep_agent(
    agent_module: str,
    storage_root: Path,
    on_row: Optional[Callable[[AblationRow], None]] = None,
) -> list[AblationRow]:
    """Run all 1 + N + N configurations for one agent.

    ``on_row`` is invoked after each configuration finishes so callers
    can flush results to disk incrementally — a partial sweep then
    survives a crash or budget cutoff.
    """
    rows: list[AblationRow] = []

    def add(row: AblationRow) -> None:
        rows.append(row)
        if on_row is not None:
            on_row(row)

    add(
        _run_config(
            agent_module=agent_module,
            config="all-on",
            rules_off=[],
            storage_root=storage_root,
        )
    )
    for rule in RULES:
        add(
            _run_config(
                agent_module=agent_module,
                config=f"{rule}-off",
                rules_off=[rule],
                storage_root=storage_root,
            )
        )
    for rule in RULES:
        others = [r for r in RULES if r != rule]
        add(
            _run_config(
                agent_module=agent_module,
                config=f"{rule}-only",
                rules_off=others,
                storage_root=storage_root,
            )
        )
    return rows


_CSV_COLUMNS = [
    "agent_module",
    "config",
    "cost_savings_pct",
    "accuracy_delta_pp",
    "baseline_cost_usd",
    "optimized_cost_usd",
    "n_tasks",
    "n_passed_optimized",
]


def _row_values(r: AblationRow) -> list:
    return [
        r.agent_module,
        r.config,
        f"{r.cost_savings_pct:.3f}",
        f"{r.accuracy_delta_pp:.3f}",
        f"{r.baseline_cost_usd:.6f}",
        f"{r.optimized_cost_usd:.6f}",
        r.n_tasks,
        r.n_passed_optimized,
    ]


def write_header(out_path: Path) -> None:
    """Write CSV header (truncates the file)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as f:
        csv.writer(f).writerow(_CSV_COLUMNS)


def append_row(row: AblationRow, out_path: Path) -> None:
    """Append a single row to an already-headered CSV, fsync'ing to disk."""
    with out_path.open("a", newline="") as f:
        csv.writer(f).writerow(_row_values(row))
        f.flush()
        os.fsync(f.fileno())


def write_csv(rows: list[AblationRow], out_path: Path) -> None:
    """One-shot writer for callers that already have all rows in memory."""
    write_header(out_path)
    for r in rows:
        append_row(r, out_path)


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m bench.optimizer_ablation",
        description="Run a (rule × agent) contribution-matrix sweep.",
    )
    p.add_argument(
        "agents",
        nargs="+",
        help=(
            "One or more agent module paths, "
            "e.g. bench.agents.gaia_router bench.agents.rag_summarizer"
        ),
    )
    p.add_argument(
        "--storage-root",
        default="/tmp/agentc-ablation",
        help="Root dir for per-config storage (default: /tmp/agentc-ablation)",
    )
    p.add_argument(
        "--out",
        default="bench/results/ablation.csv",
        help="Output CSV path (default: bench/results/ablation.csv)",
    )
    args = p.parse_args(argv)

    out = Path(args.out)
    write_header(out)
    n_written = 0

    def flush(row: AblationRow) -> None:
        nonlocal n_written
        append_row(row, out)
        n_written += 1
        print(f"  [{n_written}] {row.agent_module} / {row.config} → {out}")

    for agent in args.agents:
        agent_root = Path(args.storage_root) / agent.replace(".", "_")
        if agent_root.exists():
            shutil.rmtree(agent_root)
        sweep_agent(agent, agent_root, on_row=flush)

    print(f"\nWrote {n_written} rows to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
