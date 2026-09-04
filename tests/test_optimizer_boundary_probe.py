"""Regression coverage for optimizer boundary/scheduler attribution."""

from __future__ import annotations

import gzip
from pathlib import Path

from bench.optimizer_boundary_probe import (
    _OPERATIONS,
    _measure_concurrent_operation,
    _write_raw,
    run,
)


def test_concurrent_probe_is_balanced_and_records_enclosed_clocks() -> None:
    group_ns, rows = _measure_concurrent_operation(
        "python_noop", calls=10, concurrency=3
    )

    assert group_ns > 0
    assert [row["sequence"] for row in rows] == list(range(10))
    assert all(row["wall_ns"] >= row["thread_cpu_ns"] for row in rows)
    assert all(row["off_cpu_ns"] >= 0 for row in rows)


def test_small_boundary_probe_covers_every_operation_and_concurrency() -> None:
    result, raw = run(
        concurrencies=(1, 2),
        calls_per_cell=8,
        warmup=2,
        replications=1,
        bootstrap_resamples=100,
        build_profile="debug",
    )

    assert result["paper_evidence"] is False
    assert result["network_calls"] == 0
    assert len(raw) == len(_OPERATIONS) * 2 * 8
    aggregates = result["aggregate_measurements_us_and_throughput"]
    assert len(aggregates) == len(_OPERATIONS) * 2
    assert all(row["sample_count"] == 8 for row in aggregates)
    assert all(row["wall"]["p99_us"] >= row["wall"]["p50_us"] for row in aggregates)
    assert all(
        row["thread_cpu"]["p99_us"] >= row["thread_cpu"]["p50_us"]
        and row["off_cpu"]["p99_us"] >= row["off_cpu"]["p50_us"]
        for row in aggregates
    )


def test_raw_gzip_is_reproducible_and_uses_lf(tmp_path: Path) -> None:
    first = tmp_path / "first.csv.gz"
    second = tmp_path / "second.csv.gz"
    _write_raw(first, [])
    _write_raw(second, [])

    assert first.read_bytes() == second.read_bytes()
    with gzip.open(first, "rb") as handle:
        payload = handle.read()
    assert payload.endswith(b"\n")
    assert b"\r" not in payload
