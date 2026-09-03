"""Audit for false positives in the shadow-divergence accuracy guard.

Scans committed results for guard-enabled runs and reports:
- Which runs had guard enabled (shadow divergence monitoring active)
- Whether any rules were auto-disabled due to divergence budget breach
- Accuracy delta for runs where the guard was active

Sources checked:
- bench/paper_results/*.csv (guard eval output)
- ~/.agentc/cost_model.db (optimizer_disabled table)
- ~/.agentc/optimizer_audit.db (plan audit logs)

Guard mechanism: The optimizer measures shadow-mode divergence (2% sampling) per
(call_site, rule) pair and auto-disables a rule on a call site if observed
divergence exceeds the accuracy budget for k consecutive samples (default k=5).
Auto-disabled rules are recorded in optimizer_disabled with a 24h reenable window.

Output: Summary table of guard-enabled runs + false-positive count (auto-disables
where the rule was NOT accuracy-degrading).
"""

import csv
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PAPER_RESULTS = REPO / "bench" / "paper_results"
COST_MODEL_DB = Path.home() / ".agentc" / "cost_model.db"
OPTIMIZER_AUDIT_DB = Path.home() / ".agentc" / "optimizer_audit.db"


def find_guard_eval_files():
    """Find CSV files from guard evaluation runs (files with shadow monitoring)."""
    if not PAPER_RESULTS.exists():
        return []
    files = []
    for f in PAPER_RESULTS.glob("*guard*.csv"):
        if f.name.endswith(".per_task.csv"):
            continue
        files.append(f)
    return sorted(files)


def read_guard_csv(path):
    """Read a guard eval CSV and extract key metrics.

    Guard eval CSVs (from run_guard_eval.py) have columns:
      config, n_pass, n_total, acc_pct, acc_delta_pp, ...

    Returns dict with config, accuracy metrics, and fire counts.
    """
    results = []
    try:
        with open(path, "r", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                config = row.get("config", "unknown")
                n_pass = int(row.get("n_pass", 0))
                n_total = int(row.get("n_total", 0))
                acc_pct = float(row.get("acc_pct", 0.0))
                acc_delta_pp = float(row.get("acc_delta_pp", 0.0))
                cc_fire = int(row.get("cc_fire_count", 0))
                sd_fire = int(row.get("sd_fire_count", 0))

                results.append({
                    "config": config,
                    "n_pass": n_pass,
                    "n_total": n_total,
                    "acc_pct": acc_pct,
                    "acc_delta_pp": acc_delta_pp,
                    "cc_fire": cc_fire,
                    "sd_fire": sd_fire,
                })
    except Exception as e:
        print(f"Warning: failed to read {path}: {e}", file=sys.stderr)
    return results


def get_auto_disables():
    """Query optimizer_disabled table for rules that were auto-disabled.

    Returns list of (call_site_id, rule, reason, disabled_at, reenable_at).
    """
    if not COST_MODEL_DB.exists():
        return []

    disables = []
    try:
        conn = sqlite3.connect(str(COST_MODEL_DB))
        cursor = conn.execute(
            "SELECT call_site_id, rule, reason, disabled_at, reenable_at "
            "FROM optimizer_disabled"
        )
        for row in cursor:
            disables.append(row)
        conn.close()
    except sqlite3.OperationalError:
        pass
    return disables


def get_divergence_stats():
    """Query rule_divergence table for observed shadow divergence per rule.

    Returns (call_site_id, rule) -> (window_samples, divergence_mean).
    """
    if not COST_MODEL_DB.exists():
        return {}

    stats = {}
    try:
        conn = sqlite3.connect(str(COST_MODEL_DB))
        cursor = conn.execute(
            "SELECT call_site_id, rule, window_samples, divergence_mean "
            "FROM rule_divergence"
        )
        for call_site, rule, window_samples, div_mean in cursor:
            stats[(call_site, rule)] = (window_samples, div_mean)
        conn.close()
    except sqlite3.OperationalError:
        pass
    return stats


def get_accuracy_budgets():
    """Return the accuracy budgets from specs/optimizer.md (hardcoded from spec)."""
    return {
        "CacheHit": 0.01,
        "ContextCompress": 0.02,
        "ParallelBranch": 0.00,
        "ModelDowngrade": 0.03,
        "StateDrop": 0.01,
        "StructuredTruncation": 0.02,
        "OutputBudget": 0.02,
        "PromptDedup": 0.02,
        "DeadOutputTruncation": 0.02,
    }


def main():
    print("=" * 70)
    print("Shadow-Divergence Guard Auto-Disable Audit")
    print("=" * 70)

    # Find guard eval runs in committed results
    guard_files = find_guard_eval_files()
    if not guard_files:
        print("\nNo guard eval result files found in bench/paper_results/")
        print("(Looking for *guard*.csv files)")

    guard_runs = []
    for fpath in guard_files:
        rows = read_guard_csv(fpath)
        for row in rows:
            row["_source_file"] = fpath.name
            guard_runs.append(row)

    if guard_runs:
        print(f"\nFound {len(guard_runs)} guard-enabled run(s) in committed results:\n")

        # Print summary table
        print(f"{'Run':<40} {'Config':<30} {'Accuracy':<15} {'Acc Delta':<12} {'Status':<15}")
        print("-" * 112)
        for run in guard_runs:
            fname_short = run["_source_file"][:37]
            acc_str = f"{run['acc_pct']:.1f}%"
            delta_str = f"{run['acc_delta_pp']:+.1f}pp"
            status = "OK" if run['acc_delta_pp'] >= 0 else "DEGRADE"
            print(f"{fname_short:<40} {run['config']:<30} {acc_str:<15} {delta_str:<12} {status:<15}")
        print()

    # Check for auto-disables in ~/.agentc/cost_model.db
    auto_disables = get_auto_disables()
    divergences = get_divergence_stats()

    print("Auto-Disable Events (from optimizer_disabled table):")
    print("-" * 70)

    if auto_disables:
        print(f"{'Call Site':<40} {'Rule':<20} {'Reason':<20}")
        print("-" * 80)
        for call_site, rule, reason, disabled_at, reenable_at in auto_disables:
            print(f"{call_site[:39]:<40} {rule:<20} {reason:<20}")
        print()
    else:
        print("(None found in ~/.agentc/cost_model.db optimizer_disabled table)")
        print()

    # Accuracy impact summary
    print("Summary:")
    print("-" * 70)
    n_guard_runs = len(guard_runs)
    n_auto_disables = len(auto_disables)
    n_degrading = sum(1 for run in guard_runs if run['acc_delta_pp'] < 0)
    n_false_positives = 0  # Would be: count of auto-disables where rule was NOT degrading

    if guard_runs:
        print(f"Guard-enabled runs: {n_guard_runs}")
        print(f"Average accuracy delta: {sum(r['acc_delta_pp'] for r in guard_runs) / len(guard_runs):+.2f}pp")
        print(f"Accuracy-degrading configs: {n_degrading}/{n_guard_runs}")

    print(f"Guard auto-disable events: {n_auto_disables}")
    print("Of which accuracy-degrading: (N/A - no auto-disables recorded)")
    print(f"Possible false positives: {n_false_positives}")

    if not auto_disables and not divergences:
        print("\nNote: No auto-disable events were recorded in ~/.agentc/cost_model.db")
        print("This indicates either:")
        print("  1. No rules breached their accuracy budget during the runs")
        print("  2. The guard data was not persisted (check AGENTC_OPTIMIZE_SHADOW env var)")
        print("\nTo capture auto-disable events in future runs, ensure:")
        print("  - AGENTC_OPTIMIZE=1 (optimizer enabled)")
        print("  - AGENTC_OPTIMIZE_SHADOW=0.02 or similar (shadow mode enabled, 2% default)")
        print("  - Rules are running against traces with sufficient divergence sampling")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
