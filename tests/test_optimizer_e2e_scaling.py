"""Regression coverage for the optimizer size/concurrency benchmark."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from bench.optimizer_e2e_scaling import (
    _partition_calls,
    _plan_result,
    _sized_call_json,
    _write_raw,
    run,
)


@pytest.mark.parametrize("target_bytes", [4_096, 8_192, 65_536])
def test_sized_call_json_has_exact_utf8_size(target_bytes: int) -> None:
    payload = _sized_call_json("test.site", target_bytes=target_bytes, span_number=7)

    assert len(payload.encode("utf-8")) == target_bytes
    decoded = json.loads(payload)
    assert decoded["span_id"] == "0000000000000007"
    assert decoded["call_site_id"] == "test.site"


def test_partition_calls_is_balanced_and_lossless() -> None:
    calls = [(index, str(index)) for index in range(10)]
    partitions = _partition_calls(calls, 3)

    assert [len(partition) for partition in partitions] == [4, 3, 3]
    assert sorted(row for partition in partitions for row in partition) == calls


def test_plan_result_preserves_an_overhead_fallback_reason() -> None:
    plan_json = json.dumps(
        {
            "kind": "pass_through",
            "agentc_planner_diagnostics": {
                "fallback_reason": "planning_overhead_exceeded"
            },
        }
    )

    assert _plan_result(plan_json) == (
        "pass_through",
        "planning_overhead_exceeded",
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


def test_small_run_pairs_concurrent_calls_by_span() -> None:
    result, raw = run(
        context_kib=(4,),
        concurrencies=(1, 2),
        calls_per_cell=8,
        warmup=2,
        replications=1,
        bootstrap_resamples=100,
        max_overhead_ms=1_000.0,
        build_profile="debug",
    )

    assert result["paper_evidence"] is False
    assert result["network_calls"] == 0
    assert len(raw) == 32
    assert (
        len({(row["scenario"], row["concurrency"], row["span_id"]) for row in raw})
        == 32
    )
    assert all(row["e2e_ns"] >= row["internal_pre_audit_ns"] for row in raw)
    assert all(row["boundary_state_audit_residual_ns"] >= 0 for row in raw)
    assert not any(row["fell_back_from_expected"] for row in raw)
    aggregates = result["aggregate_measurements_us_and_throughput"]
    assert len(aggregates) == 4
    assert all(row["sample_count"] == 8 for row in aggregates)
    assert all(row["replication_count"] == 1 for row in aggregates)
    assert all(row["fallback_rate_pct"] == 0.0 for row in aggregates)
    assert all(row["e2e"]["p99_us"] >= row["e2e"]["p50_us"] for row in aggregates)
    assert all(
        replication["audit_journal_mode"] == "wal"
        for replication in result["replications"]
    )
