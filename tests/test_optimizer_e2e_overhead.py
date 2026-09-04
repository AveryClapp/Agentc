"""Regression coverage for the true end-to-end optimizer benchmark."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from bench.optimizer_e2e_overhead import (
    _bootstrap_mean_ci,
    _reset_native_optimizer,
    _summarize_ns,
    _write_raw,
    run,
)


def test_native_optimizer_is_reset_after_benchmark_failure() -> None:
    resets: list[None] = []
    caught: RuntimeError | None = None
    with patch(
        "bench.optimizer_e2e_overhead._native.optimize_reset",
        new=lambda: resets.append(None),
    ):
        try:
            with _reset_native_optimizer():
                raise RuntimeError("forced failure")
        except RuntimeError as error:
            caught = error

    assert caught is not None
    assert str(caught) == "forced failure"
    assert resets == [None, None]


def test_raw_csv_uses_repository_native_lf_endings(tmp_path: Path) -> None:
    path = tmp_path / "raw.csv"
    _write_raw(path, [])

    payload = path.read_bytes()
    assert payload.endswith(b"\n")
    assert b"\r" not in payload


def test_summarize_ns_reports_expected_order_statistics() -> None:
    summary = _summarize_ns([1_000, 2_000, 3_000, 4_000, 5_000])
    assert summary == {
        "mean_us": 3.0,
        "p50_us": 3.0,
        "p95_us": 5.0,
        "p99_us": 5.0,
        "max_us": 5.0,
    }


def test_bootstrap_is_seeded_and_contains_the_estimate() -> None:
    first = _bootstrap_mean_ci([1.0, 2.0, 3.0], resamples=500, seed=7)
    second = _bootstrap_mean_ci([1.0, 2.0, 3.0], resamples=500, seed=7)
    assert first == second
    assert first["estimate"] == 2.0
    assert first["ci95_low"] <= first["estimate"] <= first["ci95_high"]


def test_small_run_pairs_every_ffi_call_with_one_wal_audit_row() -> None:
    result, raw = run(
        iterations=20,
        warmup=5,
        replications=1,
        bootstrap_resamples=100,
        max_overhead_ms=1_000.0,
        build_profile="debug",
    )

    assert result["paper_evidence"] is False
    assert result["network_calls"] == 0
    assert result["agentc_git_commit"]
    assert len(raw) == 60
    assert {row["scenario"] for row in raw} == {
        "joint_reference",
        "joint_admitted_rewrite",
        "legacy_greedy_rewrite",
    }
    assert all(row["e2e_ns"] >= row["internal_pre_audit_ns"] for row in raw)
    assert all(row["boundary_state_audit_residual_ns"] >= 0 for row in raw)
    for scenario in result["aggregate_measurements_us"].values():
        assert scenario["sample_count"] == 20
        assert scenario["replication_count"] == 1
        assert scenario["e2e"]["p99_us"] >= scenario["e2e"]["p50_us"]
    assert all(
        replication["audit_journal_mode"] == "wal"
        for replication in result["replications"]
    )
