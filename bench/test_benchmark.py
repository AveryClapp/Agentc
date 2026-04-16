"""Tests for the benchmark suite (bd-38o).

Run: maturin develop && pytest bench/test_benchmark.py -v
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import agentc
from agentc._context import set_current_span
from agentc._lifecycle import _initialized, _shutdown_in_progress

from bench.calibrate import (
    CalibrationReport,
    DetectorCalibration,
    EmbeddingPair,
    ThresholdResult,
    calibrate_redundant_call,
    calibrate_retry_storm,
    cosine_similarity,
    evaluate_threshold,
    generate_calibration_pairs,
    generate_embedding,
    generate_similar_embedding,
    run_calibration,
)
from bench.harness import (
    MockPipelineTrace,
    TaskSplit,
    generate_pipeline_trace,
)
from bench.overhead import (
    OverheadResult,
    measure_per_call_overhead,
)
from bench.report import BenchmarkReport, generate_report, save_report
from bench.validate import (
    CorrectnessReport,
    ValidationResult,
    validate_call_capture,
    validate_span_tree,
)


@pytest.fixture(autouse=True)
def _clean_state() -> Any:
    """Reset agentc state between tests."""
    _initialized.clear()
    _shutdown_in_progress.clear()
    set_current_span(None)
    yield
    if agentc.is_initialized():
        agentc.shutdown()
    _initialized.clear()
    _shutdown_in_progress.clear()


# ──────────────────── Harness Tests ────────────────────


class TestTaskSplit:
    def test_deterministic_split(self) -> None:
        s1 = TaskSplit.create(seed=42)
        s2 = TaskSplit.create(seed=42)
        assert s1.calibration == s2.calibration
        assert s1.validation == s2.validation
        assert s1.overhead == s2.overhead

    def test_split_sizes(self) -> None:
        s = TaskSplit.create(seed=42)
        assert len(s.calibration) == 20
        assert len(s.validation) == 20
        assert len(s.overhead) == 10

    def test_no_overlap(self) -> None:
        s = TaskSplit.create(seed=42)
        all_tasks = set(s.calibration) | set(s.validation) | set(s.overhead)
        assert len(all_tasks) == 50  # No duplicates

    def test_different_seeds_different_splits(self) -> None:
        s1 = TaskSplit.create(seed=42)
        s2 = TaskSplit.create(seed=99)
        assert s1.calibration != s2.calibration


class TestPipelineTrace:
    def test_deterministic(self) -> None:
        t1 = generate_pipeline_trace("task-001", seed=42)
        t2 = generate_pipeline_trace("task-001", seed=42)
        assert t1.total_calls == t2.total_calls
        assert t1.total_input_tokens == t2.total_input_tokens

    def test_has_multiple_agents(self) -> None:
        trace = generate_pipeline_trace("task-001", seed=42)
        agents = {s.agent_name for s in trace.steps}
        assert len(agents) >= 3

    def test_has_multiple_calls(self) -> None:
        trace = generate_pipeline_trace("task-001", seed=42)
        assert trace.total_calls >= 10

    def test_with_waste_patterns(self) -> None:
        trace = generate_pipeline_trace("task-001", seed=42, include_waste=True)
        # Waste steps add extra calls
        trace_clean = generate_pipeline_trace("task-001", seed=42, include_waste=False)
        assert trace.total_calls > trace_clean.total_calls

    def test_waste_patterns_include_redundant(self) -> None:
        trace = generate_pipeline_trace("task-001", seed=42, include_waste=True)
        step_names = [s.step_name for s in trace.steps]
        assert "redundant" in step_names
        assert "retry_storm" in step_names
        assert "context_bloat" in step_names
        assert "model_overkill" in step_names


# ──────────────────── Calibration Tests ────────────────────


class TestEmbeddings:
    def test_unit_normalized(self) -> None:
        import random

        rng = random.Random(42)
        emb = generate_embedding(256, rng)
        norm = math.sqrt(sum(x * x for x in emb))
        assert abs(norm - 1.0) < 1e-6

    def test_cosine_self_similarity(self) -> None:
        import random

        rng = random.Random(42)
        emb = generate_embedding(256, rng)
        assert abs(cosine_similarity(emb, emb) - 1.0) < 1e-6

    def test_generate_similar_embedding(self) -> None:
        import random

        rng = random.Random(42)
        base = generate_embedding(256, rng)
        for target in [0.80, 0.85, 0.90, 0.95]:
            similar = generate_similar_embedding(base, target, rng)
            actual = cosine_similarity(base, similar)
            # Should be within 0.02 of target
            assert abs(actual - target) < 0.02, (
                f"target={target}, actual={actual}"
            )


class TestCalibrationPairs:
    def test_pair_count(self) -> None:
        pairs = generate_calibration_pairs(n_pairs=100, seed=42)
        assert len(pairs) == 100

    def test_waste_label_distribution(self) -> None:
        pairs = generate_calibration_pairs(n_pairs=200, seed=42)
        waste = [p for p in pairs if p.is_waste]
        not_waste = [p for p in pairs if not p.is_waste]
        assert len(waste) == 100
        assert len(not_waste) == 100

    def test_similarity_range(self) -> None:
        pairs = generate_calibration_pairs(n_pairs=200, seed=42)
        for pair in pairs:
            assert 0.0 <= pair.true_similarity <= 1.0

    def test_deterministic(self) -> None:
        p1 = generate_calibration_pairs(n_pairs=50, seed=42)
        p2 = generate_calibration_pairs(n_pairs=50, seed=42)
        for a, b in zip(p1, p2):
            assert abs(a.true_similarity - b.true_similarity) < 1e-10


class TestThresholdEvaluation:
    def test_perfect_separator(self) -> None:
        """All waste above 0.95, all not-waste below 0.80 → perfect precision."""
        pairs = [
            EmbeddingPair("a", "b", [], [], 0.96, True),
            EmbeddingPair("c", "d", [], [], 0.97, True),
            EmbeddingPair("e", "f", [], [], 0.70, False),
            EmbeddingPair("g", "h", [], [], 0.75, False),
        ]
        result = evaluate_threshold(pairs, 0.90)
        assert result.precision == 1.0
        assert result.recall == 1.0

    def test_all_flagged(self) -> None:
        """Threshold so low everything is flagged."""
        pairs = [
            EmbeddingPair("a", "b", [], [], 0.96, True),
            EmbeddingPair("c", "d", [], [], 0.85, False),
        ]
        result = evaluate_threshold(pairs, 0.80)
        assert result.true_positives == 1
        assert result.false_positives == 1
        assert result.precision == 0.5

    def test_none_flagged(self) -> None:
        """Threshold so high nothing is flagged."""
        pairs = [
            EmbeddingPair("a", "b", [], [], 0.90, True),
            EmbeddingPair("c", "d", [], [], 0.85, False),
        ]
        result = evaluate_threshold(pairs, 0.99)
        assert result.true_positives == 0
        assert result.false_negatives == 1
        assert result.true_negatives == 1


class TestDetectorCalibration:
    def test_redundant_call_calibration(self) -> None:
        cal = calibrate_redundant_call(n_pairs=200, seed=42)
        assert cal.detector_name == "redundant_call"
        assert len(cal.results) == 4  # 4 candidate thresholds
        assert cal.recommended_threshold in [0.80, 0.85, 0.90, 0.95]

    def test_retry_storm_calibration(self) -> None:
        cal = calibrate_retry_storm(n_pairs=200, seed=42)
        assert cal.detector_name == "retry_storm"
        assert len(cal.results) == 4

    def test_precision_target(self) -> None:
        """Recommended threshold should achieve precision >= 0.85."""
        cal = calibrate_redundant_call(n_pairs=200, seed=42)
        best = None
        for r in cal.results:
            if abs(r.threshold - cal.recommended_threshold) < 1e-6:
                best = r
                break
        assert best is not None
        assert best.precision >= 0.85

    def test_full_calibration(self) -> None:
        report = run_calibration(n_calibration_tasks=20, seed=42)
        assert len(report.detectors) == 2
        assert report.passed


# ──────────────────── Overhead Tests ────────────────────


class TestOverheadResult:
    def test_budget_check(self) -> None:
        result = OverheadResult(
            task_id="test",
            total_calls=100,
            bare_wall_clock_ns=1_000_000_000,
            instrumented_wall_clock_ns=1_010_000_000,  # 1% overhead
            per_call_overhead_ns=[100_000] * 100,  # 100us each
            bare_peak_memory_bytes=10_000_000,
            instrumented_peak_memory_bytes=15_000_000,  # 5MB overhead
        )
        budget = result.passes_budget()
        assert budget["latency_per_call_under_5ms"] is True
        assert budget["wall_clock_under_5pct"] is True
        assert budget["memory_under_50mb"] is True

    def test_budget_fails_latency(self) -> None:
        result = OverheadResult(
            task_id="test",
            total_calls=10,
            bare_wall_clock_ns=1_000_000_000,
            instrumented_wall_clock_ns=1_100_000_000,
            per_call_overhead_ns=[10_000_000] * 10,  # 10ms each
            bare_peak_memory_bytes=10_000_000,
            instrumented_peak_memory_bytes=10_000_000,
            real_pipeline_ms=1000.0,  # 1s real pipeline
        )
        budget = result.passes_budget()
        assert budget["latency_per_call_under_5ms"] is False
        # 10ms * 10 calls = 100ms overhead on 1000ms pipeline = 10%
        assert budget["wall_clock_under_5pct"] is False

    def test_wall_clock_overhead_pct(self) -> None:
        # 50ns * 1 call = 50ns overhead on 0.001ms pipeline = 5%
        result = OverheadResult(
            task_id="test",
            total_calls=1,
            bare_wall_clock_ns=1000,
            instrumented_wall_clock_ns=1050,
            per_call_overhead_ns=[50],
            bare_peak_memory_bytes=0,
            instrumented_peak_memory_bytes=0,
            real_pipeline_ms=0.001,  # 1us real pipeline
        )
        assert abs(result.wall_clock_overhead_pct - 5.0) < 0.01


class TestPerCallOverhead:
    def test_overhead_under_budget(self, tmp_path: Path) -> None:
        """Per-call instrumentation overhead should be <5ms."""
        overheads = measure_per_call_overhead(
            n_iterations=50,
            tmp_storage=tmp_path / "bench",
        )
        assert len(overheads) == 50
        # P99 should be under 5ms (5_000_000 ns)
        sorted_vals = sorted(overheads)
        p99 = sorted_vals[int(len(sorted_vals) * 0.99)]
        p99_ms = p99 / 1_000_000
        assert p99_ms < 5.0, f"P99 overhead {p99_ms:.2f}ms exceeds 5ms budget"


# ──────────────────── Validation Tests ────────────────────


class TestCallCapture:
    def test_all_calls_captured(self, tmp_path: Path) -> None:
        results = validate_call_capture(
            "task-001", tmp_path / "bench", seed=42
        )
        for r in results:
            assert r.passed, f"{r.check_name}: expected={r.expected}, actual={r.actual}"


class TestSpanTree:
    def test_tree_structure(self, tmp_path: Path) -> None:
        results = validate_span_tree(
            "task-001", tmp_path / "bench", seed=42
        )
        for r in results:
            assert r.passed, f"{r.check_name}: expected={r.expected}, actual={r.actual}"


# ──────────────────── Report Tests ────────────────────


class TestReport:
    def test_generate_report(self) -> None:
        report = BenchmarkReport(
            overhead_results=[
                OverheadResult(
                    task_id="test-001",
                    total_calls=10,
                    bare_wall_clock_ns=1_000_000_000,
                    instrumented_wall_clock_ns=1_010_000_000,
                    per_call_overhead_ns=[100_000] * 10,
                    bare_peak_memory_bytes=10_000_000,
                    instrumented_peak_memory_bytes=15_000_000,
                ),
            ],
            calibration=run_calibration(seed=42),
            validation_results=[
                CorrectnessReport(
                    task_id="test-001",
                    results=[
                        ValidationResult("check1", True, 1, 1),
                        ValidationResult("check2", True, 0, 0),
                    ],
                ),
            ],
        )
        text = generate_report(report)
        assert "AGENTC BENCHMARK REPORT" in text
        assert "OVERHEAD MEASUREMENT" in text
        assert "THRESHOLD CALIBRATION" in text
        assert "CORRECTNESS VALIDATION" in text
        assert "OVERALL" in text

    def test_save_report(self, tmp_path: Path) -> None:
        report = BenchmarkReport(
            calibration=run_calibration(seed=42),
            validation_results=[
                CorrectnessReport(task_id="test-001", results=[]),
            ],
        )
        output_dir = tmp_path / "results"
        text_path = save_report(report, output_dir)
        assert text_path.exists()
        assert (output_dir / "benchmark_summary.json").exists()

        import json

        summary = json.loads((output_dir / "benchmark_summary.json").read_text())
        assert "passed" in summary
        assert "calibration" in summary

    def test_all_pass(self) -> None:
        report = BenchmarkReport(
            overhead_results=[
                OverheadResult(
                    task_id="t",
                    total_calls=1,
                    bare_wall_clock_ns=1000,
                    instrumented_wall_clock_ns=1001,
                    per_call_overhead_ns=[1000],
                    bare_peak_memory_bytes=0,
                    instrumented_peak_memory_bytes=0,
                ),
            ],
            calibration=run_calibration(seed=42),
            validation_results=[
                CorrectnessReport(
                    task_id="t",
                    results=[ValidationResult("c", True, 1, 1)],
                ),
            ],
        )
        assert report.all_passed
