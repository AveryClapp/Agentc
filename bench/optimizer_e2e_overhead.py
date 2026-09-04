"""Measure the complete native optimizer call, including audit enqueue.

The historical optimizer-overhead result comes from ``plan_audit.overhead_us``.
That clock stops immediately before ``write_plan_audit``. This benchmark pairs
that internal value with wall-clock timing around the complete
``_native.optimize_plan`` call, so the residual includes native state lookup,
the Python/Rust boundary, audit-row construction and bounded non-blocking
enqueue, and conversion of the returned Python string. The ordered persistence
flush is deliberately outside the timed request path.

No provider is called. Run a release-built extension for the committed Stage E0
artifact:

    maturin develop --release -m crates/agentc-profiler/Cargo.toml
    python -m bench.optimizer_e2e_overhead \
      --build-profile release \
      --output /tmp/optimizer-e2e-overhead.json \
      --raw-output /tmp/optimizer-e2e-overhead.csv
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import platform
import random
import sqlite3
import statistics
import subprocess
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence, cast

from agentc import _native


_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_ITERATIONS = 2_000
_DEFAULT_WARMUP = 200
_DEFAULT_REPLICATIONS = 5
_DEFAULT_BOOTSTRAP_RESAMPLES = 10_000
_BOOTSTRAP_SEED = 20_260_904
_PAIRED_EVIDENCE = 20

_SCENARIOS = (
    "joint_reference",
    "joint_admitted_rewrite",
    "legacy_greedy_rewrite",
)


def _call(site: str) -> dict[str, Any]:
    return {
        "call_site_id": site,
        "trace_id": "00" * 16,
        "span_id": "00" * 8,
        "model": "gpt-4o-2024-11-20",
        "messages": [{"role": "user", "content": "same shaped prompt"}],
        "parameters": {
            "max_output_tokens": 1_024,
            "temperature": 0.0,
            "extra": {
                "agentc_route_context": {
                    "provider_protocol": "openai.chat.completions.v1",
                    "provider_namespace": "openai",
                    "input_tokens_upper_bound": 100,
                    "input_tokens_upper_bound_basis": "json_utf8_bytes_v1",
                    "image_input": False,
                    "tool_calling": False,
                    "structured_outputs": False,
                    "streaming": False,
                }
            },
        },
        "tools": [],
        "input_deps": [{"kind": "literal"}],
        "occurrence_ix": 0,
    }


def _outcome(site: str, *, candidate: bool) -> dict[str, Any]:
    return {
        "input_tokens": 100,
        "output_tokens": 40 if candidate else 100,
        "latency_ms": 350.0 if candidate else 600.0,
        "cost_usd": 0.0008 if candidate else 0.002,
        "output_is_structured": False,
        "output_is_short": True,
        "call_site_id": site,
    }


@contextmanager
def _isolated_agentc_environment(settings: dict[str, str]) -> Iterator[None]:
    saved = {
        key: value for key, value in os.environ.items() if key.startswith("AGENTC_")
    }
    for key in list(os.environ):
        if key.startswith("AGENTC_"):
            os.environ.pop(key, None)
    os.environ.update(settings)
    try:
        yield
    finally:
        for key in list(os.environ):
            if key.startswith("AGENTC_"):
                os.environ.pop(key, None)
        os.environ.update(saved)


@contextmanager
def _reset_native_optimizer() -> Iterator[None]:
    _native.optimize_reset()
    try:
        yield
    finally:
        _native.optimize_reset()


def _plan_kind(plan_json: str) -> str:
    value = json.loads(plan_json)
    kind = value.get("kind")
    if not isinstance(kind, str):
        raise RuntimeError("optimizer returned a plan without a string kind")
    return kind


def _admit_joint_rewrite(call_json: str, candidate_outcome_json: str) -> None:
    os.environ["AGENTC_EVAL_PLANNER_MODE"] = "current_greedy"
    for _ in range(_PAIRED_EVIDENCE):
        plan_json = _native.optimize_plan(call_json)
        if _plan_kind(plan_json) != "rewritten":
            raise RuntimeError("greedy calibration did not produce OutputBudget")
        token = str(_native.optimize_observe(plan_json, candidate_outcome_json))
        if not token:
            raise RuntimeError(
                "candidate calibration did not produce an observation token"
            )
        _native.optimize_record_divergence(token, 0.0)
    os.environ["AGENTC_EVAL_PLANNER_MODE"] = "joint_guarded"
    admitted = json.loads(_native.optimize_plan(call_json))
    diagnostics = admitted.get("agentc_planner_diagnostics", {})
    if (
        admitted.get("kind") != "rewritten"
        or diagnostics.get("selected_reference") is not False
    ):
        raise RuntimeError("exact paired evidence did not admit the joint candidate")


def _summarize_ns(samples: Sequence[int]) -> dict[str, float]:
    if not samples:
        raise ValueError("cannot summarize an empty sample")
    ordered = sorted(samples)

    def percentile(fraction: float) -> float:
        index = max(0, min(len(ordered) - 1, int(len(ordered) * fraction)))
        return ordered[index] / 1_000.0

    return {
        "mean_us": statistics.mean(ordered) / 1_000.0,
        "p50_us": percentile(0.50),
        "p95_us": percentile(0.95),
        "p99_us": percentile(0.99),
        "max_us": ordered[-1] / 1_000.0,
    }


def _bootstrap_mean_ci(
    values: Sequence[float], *, resamples: int, seed: int
) -> dict[str, float]:
    if not values or resamples <= 0:
        raise ValueError("bootstrap inputs must be non-empty and positive")
    rng = random.Random(seed)
    draws = sorted(
        statistics.mean(rng.choice(values) for _ in values) for _ in range(resamples)
    )
    return {
        "estimate": statistics.mean(values),
        "ci95_low": draws[int(resamples * 0.025)],
        "ci95_high": draws[min(resamples - 1, int(resamples * 0.975))],
    }


def _validated_audit_stats(*, expected_attempted_rows: int) -> dict[str, Any]:
    stats = cast(dict[str, Any], json.loads(_native.optimize_audit_stats()))
    attempted = int(stats.get("attempted_rows", -1))
    accepted = int(stats.get("accepted_rows", -1))
    written = int(stats.get("written_rows", -1))
    pending = int(stats.get("pending_rows", -1))
    dropped_full = int(stats.get("dropped_full_rows", -1))
    dropped_disconnected = int(stats.get("dropped_disconnected_rows", -1))
    write_failed = int(stats.get("write_failed_rows", -1))
    if (
        not stats.get("available")
        or attempted != expected_attempted_rows
        or accepted + dropped_full + dropped_disconnected != attempted
        or written + write_failed + pending != accepted
        or pending != 0
        or dropped_full != 0
        or dropped_disconnected != 0
        or write_failed != 0
    ):
        raise RuntimeError(f"audit writer did not drain cleanly: {stats}")
    return stats


def _read_audit_rows(audit_path: Path, after_id: int) -> list[tuple[int, str]]:
    with sqlite3.connect(audit_path) as connection:
        rows = connection.execute(
            "SELECT overhead_us, plan_kind FROM plan_audit "
            "WHERE audit_id > ? ORDER BY audit_id",
            (after_id,),
        ).fetchall()
    return [(int(overhead_us), str(kind)) for overhead_us, kind in rows]


def _journal_mode(audit_path: Path) -> str:
    with sqlite3.connect(audit_path) as connection:
        row = connection.execute("PRAGMA journal_mode").fetchone()
    if row is None:
        raise RuntimeError("journal-mode query returned no row")
    return str(row[0]).lower()


def _measure_replication(
    *,
    root: Path,
    scenario: str,
    replication: int,
    iterations: int,
    warmup: int,
    max_overhead_ms: float,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if scenario not in _SCENARIOS:
        raise ValueError(f"unknown scenario: {scenario}")
    storage = root / f"{replication:02d}-{scenario}"
    site = f"bench.optimizer-e2e:{scenario}"
    call_json = json.dumps(_call(site), separators=(",", ":"), sort_keys=True)
    reference_outcome_json = json.dumps(
        _outcome(site, candidate=False), separators=(",", ":"), sort_keys=True
    )
    candidate_outcome_json = json.dumps(
        _outcome(site, candidate=True), separators=(",", ":"), sort_keys=True
    )
    mode = "current_greedy" if scenario == "legacy_greedy_rewrite" else "joint_guarded"
    settings = {
        "AGENTC_COMPOSE": "1",
        "AGENTC_ENABLED_RULES": "OutputBudget",
        "AGENTC_EVAL_PLANNER_MODE": mode,
        "AGENTC_OPTIMIZE": "1",
        "AGENTC_OPTIMIZE_EXPLORATION": "0",
        "AGENTC_OPTIMIZE_HOT_THRESHOLD": "3",
        "AGENTC_OPTIMIZE_MAX_OVERHEAD_MS": str(max_overhead_ms),
        "AGENTC_OPTIMIZE_MIN_PLAN_EVIDENCE": str(_PAIRED_EVIDENCE),
        "AGENTC_OPTIMIZE_OBJECTIVE": "cost",
        "AGENTC_OPTIMIZE_SHADOW": "0",
        "AGENTC_PROVIDER": "openai",
    }

    with _reset_native_optimizer(), _isolated_agentc_environment(settings):
        configured_at = time.perf_counter_ns()
        _native.optimize_configure(str(storage))
        configure_ns = time.perf_counter_ns() - configured_at

        first_at = time.perf_counter_ns()
        first_plan = _native.optimize_plan(call_json)
        first_call_ns = time.perf_counter_ns() - first_at
        if _plan_kind(first_plan) != "pass_through":
            raise RuntimeError("the first cold call was not pass-through")
        first_token = str(_native.optimize_observe(first_plan, reference_outcome_json))
        if not first_token:
            raise RuntimeError(
                "the first cold call did not produce an observation token"
            )
        for _ in range(2):
            plan_json = _native.optimize_plan(call_json)
            token = str(_native.optimize_observe(plan_json, reference_outcome_json))
            if not token:
                raise RuntimeError(
                    "reference warmup did not produce an observation token"
                )

        if scenario == "joint_admitted_rewrite":
            _admit_joint_rewrite(call_json, candidate_outcome_json)
        elif scenario == "legacy_greedy_rewrite":
            if _plan_kind(_native.optimize_plan(call_json)) != "rewritten":
                raise RuntimeError("greedy scenario did not activate OutputBudget")
        elif _plan_kind(_native.optimize_plan(call_json)) != "pass_through":
            raise RuntimeError(
                "joint reference scenario unexpectedly admitted a candidate"
            )

        expected_kind = "pass_through" if scenario == "joint_reference" else "rewritten"
        for _ in range(warmup):
            if _plan_kind(_native.optimize_plan(call_json)) != expected_kind:
                raise RuntimeError(f"{scenario} changed plan kind during warmup")

        audit_path = storage / "optimizer_audit.db"
        _native.optimize_flush()
        setup_attempted_rows = warmup + (
            24 if scenario == "joint_admitted_rewrite" else 4
        )
        audit_writer_before_measurement = _validated_audit_stats(
            expected_attempted_rows=setup_attempted_rows
        )
        # This benchmark owns a fresh DB. The flushed native write count is
        # therefore also the next audit-id boundary. Do not open Python's
        # separately linked SQLite while the native WAL writer is live: POSIX
        # advisory locks are process-scoped, so closing either SQLite library's
        # descriptor can release locks held by the other library.
        first_measured_audit_id = int(audit_writer_before_measurement["written_rows"])
        e2e_ns: list[int] = []
        returned_kinds: list[str] = []
        for _ in range(iterations):
            started_at = time.perf_counter_ns()
            plan_json = _native.optimize_plan(call_json)
            e2e_ns.append(time.perf_counter_ns() - started_at)
            returned_kinds.append(_plan_kind(plan_json))

        _native.optimize_flush()
        audit_writer = _validated_audit_stats(
            expected_attempted_rows=setup_attempted_rows + iterations
        )
        _native.optimize_reset()
        audit_rows = _read_audit_rows(audit_path, first_measured_audit_id)
        if len(audit_rows) != iterations:
            raise RuntimeError(
                f"audit lost measured calls: expected {iterations}, found {len(audit_rows)}"
            )
        audit_kinds = [kind for _, kind in audit_rows]
        if set(returned_kinds) != {expected_kind} or set(audit_kinds) != {
            expected_kind
        }:
            raise RuntimeError(
                f"{scenario} plan kind drifted: returned={set(returned_kinds)}, "
                f"audit={set(audit_kinds)}"
            )
        internal_ns = [overhead_us * 1_000 for overhead_us, _ in audit_rows]
        residual_ns = [total - internal for total, internal in zip(e2e_ns, internal_ns)]
        if min(residual_ns) < 0:
            raise RuntimeError("internal planner clock exceeded its enclosing FFI call")
        journal_mode = _journal_mode(audit_path)
        if journal_mode != "wal":
            raise RuntimeError(f"optimizer audit DB is not in WAL mode: {journal_mode}")

        raw = [
            {
                "scenario": scenario,
                "replication": replication,
                "iteration": index,
                "plan_kind": expected_kind,
                "e2e_ns": total,
                "internal_pre_audit_ns": internal,
                "boundary_state_audit_residual_ns": residual,
            }
            for index, (total, internal, residual) in enumerate(
                zip(e2e_ns, internal_ns, residual_ns), start=1
            )
        ]
        summary = {
            "scenario": scenario,
            "replication": replication,
            "plan_kind": expected_kind,
            "configure_us": configure_ns / 1_000.0,
            "first_cold_call_us": first_call_ns / 1_000.0,
            "audit_journal_mode": journal_mode,
            "audit_writer_before_measurement": audit_writer_before_measurement,
            "audit_writer": audit_writer,
            "e2e": _summarize_ns(e2e_ns),
            "internal_pre_audit": _summarize_ns(internal_ns),
            "boundary_state_audit_residual": _summarize_ns(residual_ns),
        }
    return summary, raw


def _aggregate_scenario(
    replications: Sequence[dict[str, Any]],
    raw: Sequence[dict[str, Any]],
    *,
    bootstrap_resamples: int,
) -> dict[str, Any]:
    e2e_ns = [int(row["e2e_ns"]) for row in raw]
    internal_ns = [int(row["internal_pre_audit_ns"]) for row in raw]
    residual_ns = [int(row["boundary_state_audit_residual_ns"]) for row in raw]
    e2e = _summarize_ns(e2e_ns)
    internal = _summarize_ns(internal_ns)
    residual = _summarize_ns(residual_ns)
    if e2e["mean_us"] <= 0 or internal["mean_us"] <= 0:
        raise RuntimeError("optimizer clocks must have positive mean durations")
    return {
        "sample_count": len(raw),
        "replication_count": len(replications),
        "e2e": e2e,
        "internal_pre_audit": internal,
        "boundary_state_audit_residual": residual,
        "mean_residual_share_pct": 100.0 * residual["mean_us"] / e2e["mean_us"],
        "mean_e2e_to_internal_ratio": e2e["mean_us"] / internal["mean_us"],
        "replication_mean_p50_us": _bootstrap_mean_ci(
            [float(replication["e2e"]["p50_us"]) for replication in replications],
            resamples=bootstrap_resamples,
            seed=_BOOTSTRAP_SEED,
        ),
        "replication_mean_p99_us": _bootstrap_mean_ci(
            [float(replication["e2e"]["p99_us"]) for replication in replications],
            resamples=bootstrap_resamples,
            seed=_BOOTSTRAP_SEED + 1,
        ),
    }


def _git_commit() -> str | None:
    completed = subprocess.run(
        [
            "git",
            "-c",
            "core.fsmonitor=false",
            "-C",
            str(_REPO_ROOT),
            "rev-parse",
            "HEAD",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def _git_dirty() -> bool | None:
    completed = subprocess.run(
        [
            "git",
            "-c",
            "core.fsmonitor=false",
            "-C",
            str(_REPO_ROOT),
            "status",
            "--porcelain",
            "--untracked-files=normal",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    return bool(completed.stdout.strip()) if completed.returncode == 0 else None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(
    *,
    iterations: int,
    warmup: int,
    replications: int,
    bootstrap_resamples: int,
    max_overhead_ms: float,
    build_profile: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if min(iterations, warmup, replications, bootstrap_resamples) <= 0:
        raise ValueError(
            "iteration, warmup, replication, and bootstrap counts must be positive"
        )
    if not math.isfinite(max_overhead_ms) or max_overhead_ms <= 0:
        raise ValueError("max_overhead_ms must be finite and positive")

    summaries: list[dict[str, Any]] = []
    raw: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="agentc-optimizer-e2e-") as temp:
        root = Path(temp).resolve()
        for replication in range(1, replications + 1):
            offset = (replication - 1) % len(_SCENARIOS)
            ordered_scenarios = _SCENARIOS[offset:] + _SCENARIOS[:offset]
            for scenario in ordered_scenarios:
                summary, samples = _measure_replication(
                    root=root,
                    scenario=scenario,
                    replication=replication,
                    iterations=iterations,
                    warmup=warmup,
                    max_overhead_ms=max_overhead_ms,
                )
                summaries.append(summary)
                raw.extend(samples)

    aggregate: dict[str, Any] = {}
    for scenario in _SCENARIOS:
        scenario_summaries = [row for row in summaries if row["scenario"] == scenario]
        scenario_raw = [row for row in raw if row["scenario"] == scenario]
        aggregate[scenario] = _aggregate_scenario(
            scenario_summaries,
            scenario_raw,
            bootstrap_resamples=bootstrap_resamples,
        )

    native_path = Path(str(_native.__file__))
    clock = time.get_clock_info("perf_counter")
    result = {
        "schema_version": 1,
        "experiment_kind": "optimizer_end_to_end_overhead_preflight",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "agentc_git_commit": _git_commit(),
        "agentc_git_dirty": _git_dirty(),
        "paper_evidence": False,
        "network_calls": 0,
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "sqlite": sqlite3.sqlite_version,
            "native_extension": native_path.name,
            "native_extension_sha256": _sha256(native_path),
            "operator_attested_build_profile": build_profile,
            "perf_counter": {
                "implementation": clock.implementation,
                "monotonic": clock.monotonic,
                "adjustable": clock.adjustable,
                "resolution_seconds": clock.resolution,
            },
        },
        "settings": {
            "iterations_per_replication": iterations,
            "warmup_calls_per_replication": warmup,
            "replications": replications,
            "scenario_order": "three-way rotation by replication",
            "bootstrap_resamples": bootstrap_resamples,
            "bootstrap_seed": _BOOTSTRAP_SEED,
            "max_overhead_ms": max_overhead_ms,
            "enabled_rules": ["OutputBudget"],
            "exploration_enabled": False,
            "shadow_rate": 0.0,
            "objective": "cost",
            "paired_evidence_for_admitted_scenario": _PAIRED_EVIDENCE,
        },
        "aggregate_measurements_us": aggregate,
        "replications": summaries,
        "timed_scope": [
            "Python call into _native.optimize_plan",
            "native optimizer-state lookup",
            "JSON decode and encode",
            "complete-plan enumeration, guard lookup, and selection",
            "plan_audit row construction and bounded non-blocking enqueue",
            "conversion and return of the Python plan string",
        ],
        "paired_internal_clock_scope": [
            "native planning after optimizer-state lookup",
            "optional exploration reservation (disabled in this experiment)",
            "excludes enqueue_plan_audit and the outer FFI boundary",
        ],
        "interpretation_limits": [
            "This is a repeated single-machine Stage E0 systems microbenchmark, not task-quality or savings evidence.",
            "Inputs and outcomes are deterministic synthetic records; no provider or network is invoked.",
            "The residual is paired subtraction, but it combines boundary, state lookup, audit-row construction/enqueue, clock quantization, and return conversion; it is not an audit-only timer.",
            "The build profile is operator-attested; the native extension hash pins the measured binary.",
            "The ordered audit flush and SQLite commit are outside the request-path clock; normal host scheduling remains inside it.",
            "The harness derives its audit-id boundary from flushed native counters and opens Python's separately linked SQLite only after closing the native writer.",
        ],
    }
    return result, raw


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be finite and positive")
    return parsed


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=_positive_int, default=_DEFAULT_ITERATIONS)
    parser.add_argument("--warmup", type=_positive_int, default=_DEFAULT_WARMUP)
    parser.add_argument(
        "--replications", type=_positive_int, default=_DEFAULT_REPLICATIONS
    )
    parser.add_argument(
        "--bootstrap-resamples",
        type=_positive_int,
        default=_DEFAULT_BOOTSTRAP_RESAMPLES,
    )
    parser.add_argument("--max-overhead-ms", type=_positive_float, default=5.0)
    parser.add_argument(
        "--build-profile", choices=("debug", "release", "unknown"), default="unknown"
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--raw-output", type=Path)
    return parser.parse_args(argv)


def _write_raw(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=(
                "scenario",
                "replication",
                "iteration",
                "plan_kind",
                "e2e_ns",
                "internal_pre_audit_ns",
                "boundary_state_audit_residual_ns",
            ),
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    result, raw = run(
        iterations=args.iterations,
        warmup=args.warmup,
        replications=args.replications,
        bootstrap_resamples=args.bootstrap_resamples,
        max_overhead_ms=args.max_overhead_ms,
        build_profile=args.build_profile,
    )
    if args.raw_output is not None:
        _write_raw(args.raw_output, raw)
        result["raw_samples"] = {
            "path": args.raw_output.name,
            "rows": len(raw),
            "sha256": _sha256(args.raw_output),
        }
    payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        print(payload, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
