"""Baseline-vs-optimized runner for one reference agent.

Two subprocess runs of a reference agent under ``agentc record``:

    AGENTC_OPTIMIZE=0    →  baseline storage dir
    AGENTC_OPTIMIZE=1    →  optimized storage dir

Aggregates cost (from traces.db) and accuracy (from the agent's stdout
PASS/FAIL lines) and emits a structured ``BenchResult``.

This is the code-only half of O9; reaching the ship-gate savings floor
requires real API keys + real datasets. Without either, the runner
still works end-to-end — it just reports stub-mode numbers so you can
see the pipeline is wired correctly.

Usage:

    python -m bench.optimizer_bench bench.agents.gaia_router
    python -m bench.optimizer_bench bench.agents.gaia_router --storage-root /tmp/o9
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class RunStats:
    """One-side (baseline or optimized) numbers."""

    total_cost_usd: float
    wall_clock_s: float
    n_tasks: int
    n_passed: int
    stub_mode: bool
    total_input_tokens: int = 0

    @property
    def pass_rate(self) -> float:
        return (self.n_passed / self.n_tasks) if self.n_tasks else 0.0


@dataclass
class ShadowDivergence:
    """Per-rule shadow-mode divergence aggregated across all call sites.

    ``mean`` and ``n_samples`` come from ``rule_divergence`` in the
    optimized side's ``cost_model.db``. ``audit_mean`` is the mean of
    ``plan_audit.shadow_divergence`` for the same rule — redundant when
    everything is working, but useful to catch a drift between the
    shadow-audit ring buffer and the aggregated estimator."""

    rule: str
    n_samples: int
    divergence_mean: float
    audit_mean: Optional[float]


@dataclass
class BenchResult:
    """Top-level result of a single agent's baseline-vs-optimized run."""

    agent_module: str
    baseline: RunStats
    optimized: RunStats
    rules_disabled: list[str] = field(default_factory=list)
    shadow_divergence: list[ShadowDivergence] = field(default_factory=list)

    @property
    def cost_savings_pct(self) -> float:
        if self.baseline.total_cost_usd <= 0:
            return 0.0
        delta = self.baseline.total_cost_usd - self.optimized.total_cost_usd
        return 100.0 * delta / self.baseline.total_cost_usd

    @property
    def input_token_savings_pct(self) -> float:
        if self.baseline.total_input_tokens <= 0:
            return 0.0
        delta = self.baseline.total_input_tokens - self.optimized.total_input_tokens
        return 100.0 * delta / self.baseline.total_input_tokens

    @property
    def accuracy_delta_pct(self) -> float:
        return 100.0 * (self.optimized.pass_rate - self.baseline.pass_rate)

    def as_dict(self) -> dict:
        return asdict(self)


_PASS_FAIL_RE = re.compile(r"^(PASS|FAIL)\s+\S+", re.MULTILINE)


def _find_agentc_binary() -> str:
    """Locate the ``agentc`` CLI. Prefers ``$AGENTC_BIN``, then the
    dev-workspace build, then ``PATH``."""
    if env := os.environ.get("AGENTC_BIN"):
        return env
    repo = Path(__file__).resolve().parent.parent
    for candidate in (
        repo / "target" / "release" / "agentc",
        repo / "target" / "debug" / "agentc",
    ):
        if candidate.is_file():
            return str(candidate)
    if found := shutil.which("agentc"):
        return found
    raise FileNotFoundError(
        "agentc binary not found — set $AGENTC_BIN, run `cargo build`, "
        "or add it to PATH"
    )


def _aggregate_from_db(db_path: Path) -> tuple[float, float, int]:
    """Return ``(total_cost_usd, wall_clock_s, total_input_tokens)`` from a traces.db.

    Missing db → ``(0.0, 0.0, 0)``. We don't backfill costs here; the Rust
    post-record hook already runs the full-cost backfill before exit."""
    if not db_path.is_file():
        return (0.0, 0.0, 0)
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0.0), "
            "       COALESCE(MAX(COALESCE(end_time, start_time)) - "
            "                MIN(start_time), 0), "
            "       COALESCE(SUM(input_tokens), 0) "
            "FROM spans"
        ).fetchone()
    finally:
        conn.close()
    cost, duration_us, input_tokens = row
    return (float(cost), float(duration_us) / 1_000_000.0, int(input_tokens))


def _read_shadow_divergence(storage_dir: Path) -> list[ShadowDivergence]:
    """Read per-rule shadow divergence from a storage dir.

    Joins ``cost_model.db:rule_divergence`` (the aggregated estimator)
    with ``optimizer_audit.db:plan_audit`` (the ring buffer) so we can
    see both sources in one view. Returns ``[]`` if either DB is missing
    — that's expected on the baseline side, where the optimizer never
    ran."""
    cost_db = storage_dir / "cost_model.db"
    audit_db = storage_dir / "optimizer_audit.db"
    if not cost_db.is_file():
        return []

    rows: dict[str, ShadowDivergence] = {}
    conn = sqlite3.connect(str(cost_db))
    try:
        for rule, n, mean in conn.execute(
            "SELECT rule, SUM(n_samples), "
            "       CASE WHEN SUM(n_samples) > 0 "
            "            THEN SUM(divergence_mean * n_samples) / SUM(n_samples) "
            "            ELSE 0.0 END "
            "FROM rule_divergence GROUP BY rule"
        ):
            rows[rule] = ShadowDivergence(
                rule=rule,
                n_samples=int(n or 0),
                divergence_mean=float(mean or 0.0),
                audit_mean=None,
            )
    finally:
        conn.close()

    if audit_db.is_file():
        conn = sqlite3.connect(str(audit_db))
        try:
            for rule, audit_mean in conn.execute(
                "SELECT rule, AVG(shadow_divergence) "
                "FROM plan_audit "
                "WHERE shadow_sampled = 1 AND rule IS NOT NULL "
                "GROUP BY rule"
            ):
                if rule in rows:
                    rows[rule].audit_mean = (
                        float(audit_mean) if audit_mean is not None else None
                    )
                else:
                    rows[rule] = ShadowDivergence(
                        rule=rule,
                        n_samples=0,
                        divergence_mean=0.0,
                        audit_mean=(
                            float(audit_mean) if audit_mean is not None else None
                        ),
                    )
        finally:
            conn.close()

    return sorted(rows.values(), key=lambda r: r.rule)


def _parse_pass_fail(stdout: str) -> tuple[int, int]:
    """Extract ``(n_total, n_passed)`` from the agent's PASS/FAIL lines."""
    n_total = 0
    n_passed = 0
    for m in _PASS_FAIL_RE.finditer(stdout):
        n_total += 1
        if m.group(1) == "PASS":
            n_passed += 1
    return (n_total, n_passed)


def _run_side(
    *,
    agent_module: str,
    storage_dir: Path,
    optimize: bool,
    extra_env: Optional[dict[str, str]] = None,
) -> RunStats:
    storage_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["AGENTC_OPTIMIZE"] = "1" if optimize else "0"
    if extra_env:
        env.update(extra_env)

    agentc_bin = _find_agentc_binary()
    cmd = [
        agentc_bin,
        "record",
        "--storage-path",
        str(storage_dir),
        "--",
        sys.executable,
        "-m",
        agent_module,
    ]
    proc = subprocess.run(
        cmd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    # Agent stdout is captured for pass/fail parsing; emit it verbatim so
    # the harness log still reflects what the agent said.
    sys.stdout.write(proc.stdout)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise RuntimeError(
            f"agent {agent_module} failed "
            f"(exit={proc.returncode}, optimize={optimize})"
        )

    n_total, n_passed = _parse_pass_fail(proc.stdout)
    cost, wall, input_tokens = _aggregate_from_db(storage_dir / "traces.db")
    stub_mode = not os.environ.get("OPENAI_API_KEY")
    return RunStats(
        total_cost_usd=cost,
        wall_clock_s=wall,
        n_tasks=n_total,
        n_passed=n_passed,
        stub_mode=stub_mode,
        total_input_tokens=input_tokens,
    )


def run_bench(
    *,
    agent_module: str,
    storage_root: Path,
    extra_env: Optional[dict[str, str]] = None,
    rules_disabled: Optional[list[str]] = None,
    shared_baseline: Optional[RunStats] = None,
) -> BenchResult:
    """Run ``agent_module`` twice and diff. Returns a :class:`BenchResult`.

    If ``shared_baseline`` is provided the baseline subprocess is skipped and
    the pre-computed :class:`RunStats` is used directly. This lets
    ``sweep_agent`` run the baseline once and share it across all ablation
    configs, eliminating inter-config baseline variance."""
    baseline_dir = storage_root / "baseline"
    optimized_dir = storage_root / "optimized"
    if shared_baseline is not None:
        baseline = shared_baseline
    else:
        baseline = _run_side(
            agent_module=agent_module,
            storage_dir=baseline_dir,
            optimize=False,
            extra_env=extra_env,
        )
    optimized = _run_side(
        agent_module=agent_module,
        storage_dir=optimized_dir,
        optimize=True,
        extra_env=extra_env,
    )
    return BenchResult(
        agent_module=agent_module,
        baseline=baseline,
        optimized=optimized,
        rules_disabled=list(rules_disabled or []),
        shadow_divergence=_read_shadow_divergence(optimized_dir),
    )


def render_result(result: BenchResult) -> str:
    lines = [
        f"Agent:      {result.agent_module}",
        f"Baseline:   ${result.baseline.total_cost_usd:.4f}  "
        f"pass {result.baseline.n_passed}/{result.baseline.n_tasks}  "
        f"{result.baseline.wall_clock_s:.2f}s",
        f"Optimized:  ${result.optimized.total_cost_usd:.4f}  "
        f"pass {result.optimized.n_passed}/{result.optimized.n_tasks}  "
        f"{result.optimized.wall_clock_s:.2f}s",
        f"Savings:    {result.cost_savings_pct:+.1f}%  "
        f"Input-tok Δ: {result.input_token_savings_pct:+.1f}%  "
        f"Accuracy Δ: {result.accuracy_delta_pct:+.1f} pp",
    ]
    if result.rules_disabled:
        lines.append("Rules off:  " + ", ".join(result.rules_disabled))
    if result.shadow_divergence:
        lines.append("Shadow divergence (per rule):")
        for sd in result.shadow_divergence:
            audit = (
                f"{sd.audit_mean:.4f}" if sd.audit_mean is not None else "—"
            )
            lines.append(
                f"  {sd.rule:<16}  n={sd.n_samples:<5d}  "
                f"mean={sd.divergence_mean:.4f}  audit={audit}"
            )
    if result.baseline.stub_mode:
        lines.append("Mode:       STUB (no OPENAI_API_KEY — cost figures are $0)")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m bench.optimizer_bench",
        description=(
            "Run a reference agent under baseline and optimized modes; "
            "report cost + accuracy deltas."
        ),
    )
    p.add_argument(
        "agent_module",
        help="Python module path, e.g. bench.agents.gaia_router",
    )
    p.add_argument(
        "--storage-root",
        default="/tmp/agentc-bench",
        help="Root dir for baseline/optimized storage (default: /tmp/agentc-bench)",
    )
    args = p.parse_args(argv)

    root = Path(args.storage_root) / args.agent_module.replace(".", "_")
    if root.exists():
        shutil.rmtree(root)
    result = run_bench(agent_module=args.agent_module, storage_root=root)
    print(render_result(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
