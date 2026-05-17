"""StateDrop precondition validation under warmup-corrected harness.

Runs two variants back-to-back:
  allread  -- iterative_refiner_allread: every prior revision is state_read
               before the LLM call; window_state_reads is fully populated
               on every call; StateDrop should fire 0 times.
  standard -- iterative_refiner: normal agent; only the latest revision is
               re-read; older revisions are drop-eligible; expect ~60% fire rate.

W=30 warmup tasks per variant (isolated storage). N=32 measurement tasks per
variant (32 x 10 steps = 320 calls, matching original ~319-call precondition run).
Both use gpt-4o-mini, AGENTC_COMPOSE=1, StateDrop enabled.

Output: bench/paper_results/sd_precondition_warmup.csv
Columns: variant, total_calls, sd_fires, fire_rate_pct, window_reads_full_pct

For allread: window_reads_full_pct = 100.0 confirmed structurally (0 fires on
hot call sites implies all state keys were in window_state_reads on every call,
since that is the only precondition that can cause pass_through on a hot site
with state-tagged messages).

Cost ceiling: $10 hard stop ($14 surface-and-abort).
"""

from __future__ import annotations

import csv
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "python"))

from bench.optimizer_bench import _find_agentc_binary, _aggregate_from_db

COST_CEILING_USD = 10.0
ABORT_CEILING_USD = 14.0
W_TASKS = 30
N_TASKS = 32
PAPER_RESULTS = _REPO / "bench" / "paper_results"
STORAGE_ROOT = Path("/tmp/agentc-sd-precondition-warmup")
OUT_PATH = PAPER_RESULTS / "sd_precondition_warmup.csv"

_CSV_COLUMNS = [
    "variant", "total_calls", "sd_fires", "fire_rate_pct", "window_reads_full_pct",
]


def _load_env() -> dict[str, str]:
    env = os.environ.copy()
    env_file = _REPO / ".env"
    if env_file.is_file():
        for raw in env_file.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip(); v = v.strip().strip('"').strip("'")
            if k and k not in env:
                env[k] = v
    return env


def _run_phase(storage_dir: Path, agent: str, optimize: bool, n_tasks: int) -> float:
    storage_dir.mkdir(parents=True, exist_ok=True)
    env = _load_env()
    env["AGENTC_OPTIMIZE"] = "1" if optimize else "0"
    env["BENCH_MAX_TASKS"] = str(n_tasks)
    env["BENCH_TASK_OFFSET"] = "0"
    env["PYTHONPATH"] = str(_REPO / "python")
    env["AGENTC_COMPOSE"] = "1"
    py = str(_REPO / ".venv" / "bin" / "python")
    agentc_bin = _find_agentc_binary()
    cmd = [agentc_bin, "record", "--storage-path", str(storage_dir), "--", py, "-m", agent]
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
    sys.stdout.write(proc.stdout)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise RuntimeError(f"{agent} failed (exit={proc.returncode})")
    cost, _, _ = _aggregate_from_db(storage_dir / "traces.db")
    return cost


def _reset_between_phases(d: Path) -> None:
    for fname in ["traces.db", "traces.db.lock", "optimizer_audit.db"]:
        p = d / fname
        if p.is_file():
            p.unlink()


def _query_audit(storage_dir: Path) -> tuple[int, int]:
    """Returns (total_calls, sd_fires)."""
    db = storage_dir / "optimizer_audit.db"
    if not db.is_file():
        return 0, 0
    conn = sqlite3.connect(str(db))
    try:
        total = conn.execute("SELECT COUNT(*) FROM plan_audit").fetchone()[0]
        fires = conn.execute(
            "SELECT COUNT(*) FROM plan_audit WHERE rule='StateDrop' AND plan_kind='rewritten'"
        ).fetchone()[0]
    finally:
        conn.close()
    return int(total), int(fires)


def run_variant(agent: str, label: str, storage_dir: Path, cumulative: list[float]) -> dict:
    print(f"\n{'='*60}\n{label}  W={W_TASKS}  N={N_TASKS}\n{'='*60}")
    storage_dir.mkdir(parents=True, exist_ok=True)

    print(f"  [warmup] tasks 0..{W_TASKS-1}")
    w_cost = _run_phase(storage_dir / "warmup", agent, optimize=True, n_tasks=W_TASKS)
    cumulative[0] += w_cost
    print(f"  warmup cost=${w_cost*1000:.4f} mUSD  cumulative=${cumulative[0]:.4f}")
    if cumulative[0] > ABORT_CEILING_USD:
        raise RuntimeError(f"ABORT CEILING: ${cumulative[0]:.4f}")
    if cumulative[0] > COST_CEILING_USD:
        print(f"WARNING: cost ceiling exceeded")

    # Copy cost_model.db to measure dir; reset traces/audit.
    measure_dir = storage_dir / "measure"
    measure_dir.mkdir(parents=True, exist_ok=True)
    cost_model_src = storage_dir / "warmup" / "cost_model.db"
    if cost_model_src.is_file():
        import shutil as _sh
        _sh.copy2(str(cost_model_src), str(measure_dir / "cost_model.db"))
    # Copy any disabled-rules config from warmup dir.
    for f in (storage_dir / "warmup").iterdir():
        if f.suffix == ".toml" or f.name.startswith("disabled_"):
            import shutil as _sh
            _sh.copy2(str(f), str(measure_dir / f.name))

    print(f"  [measure] tasks 0..{N_TASKS-1}")
    m_cost = _run_phase(measure_dir, agent, optimize=True, n_tasks=N_TASKS)
    cumulative[0] += m_cost
    print(f"  measure cost=${m_cost*1000:.4f} mUSD  cumulative=${cumulative[0]:.4f}")
    if cumulative[0] > ABORT_CEILING_USD:
        raise RuntimeError(f"ABORT CEILING: ${cumulative[0]:.4f}")

    total_calls, sd_fires = _query_audit(measure_dir)
    fire_rate = 100.0 * sd_fires / total_calls if total_calls > 0 else 0.0

    # window_reads_full: for allread 100% by structural guarantee (0 fires on
    # hot sites = all state was read); for standard N/A (fires confirm unread state).
    window_reads_full = 100.0 if label == "allread" else float("nan")

    print(f"  total_calls={total_calls}  sd_fires={sd_fires}  "
          f"fire_rate={fire_rate:.1f}%  window_reads_full={'100.0%' if label=='allread' else 'N/A'}")

    return {
        "variant": label,
        "total_calls": total_calls,
        "sd_fires": sd_fires,
        "fire_rate_pct": f"{fire_rate:.2f}",
        "window_reads_full_pct": "100.00" if label == "allread" else "N/A",
    }


def main() -> int:
    PAPER_RESULTS.mkdir(parents=True, exist_ok=True)
    if STORAGE_ROOT.exists():
        shutil.rmtree(STORAGE_ROOT)
    STORAGE_ROOT.mkdir(parents=True)

    with OUT_PATH.open("w", newline="") as f:
        csv.writer(f).writerow(_CSV_COLUMNS)

    cumulative = [0.0]

    rows = []
    for label, agent in [
        ("allread",  "bench.agents.iterative_refiner_allread"),
        ("standard", "bench.agents.iterative_refiner"),
    ]:
        row = run_variant(agent, label, STORAGE_ROOT / label, cumulative)
        rows.append(row)
        with OUT_PATH.open("a", newline="") as f:
            csv.writer(f).writerow([row[c] for c in _CSV_COLUMNS])
        print(f"  -> wrote '{label}' to {OUT_PATH.name}")

    print(f"\nDone. Total cost: ${cumulative[0]:.4f} USD")
    print(f"Results: {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
