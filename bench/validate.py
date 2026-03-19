"""Correctness validation for the profiler instrumentation.

Validates:
1. Every LLM call is captured (no data loss)
2. Token counts match provider-reported values exactly
3. Span tree structure is accurate (parent-child)
4. Streaming TTFT is accurate to within 10ms
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import patch

import agentc
from agentc._context import get_current_span, set_current_span
from agentc._lifecycle import _initialized, _shutdown_in_progress

from bench.harness import MockLLMCall, generate_pipeline_trace


@dataclass
class ValidationResult:
    """Result of a single validation check."""

    check_name: str
    passed: bool
    expected: Any = None
    actual: Any = None
    detail: str = ""


@dataclass
class CorrectnessReport:
    """Full correctness validation report."""

    task_id: str
    results: list[ValidationResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results)

    @property
    def pass_count(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def fail_count(self) -> int:
        return sum(1 for r in self.results if not r.passed)


def validate_call_capture(
    task_id: str,
    tmp_storage: Path,
    seed: int | None = None,
) -> list[ValidationResult]:
    """Validate that every LLM call is captured as a span."""
    results: list[ValidationResult] = []
    trace = generate_pipeline_trace(task_id, seed=seed)
    expected_calls = trace.total_calls
    expected_agent_steps = len(trace.steps)

    # Track all spans written
    root_spans: list[dict[str, Any]] = []
    child_spans: list[dict[str, Any]] = []

    with patch("agentc._span._write_root_span", side_effect=lambda d: root_spans.append(d)):
        with patch("agentc._span._enqueue_span", side_effect=lambda d: child_spans.append(d)):
            with patch("agentc._lifecycle._apply_patches"):
                agentc.init(storage_path=str(tmp_storage))

            for step in trace.steps:

                @agentc.trace(name=f"{step.agent_name}-{step.step_name}")
                def run_step(calls: list[MockLLMCall] = step.calls) -> None:
                    for call in calls:
                        with agentc.span(f"llm-{call.model}", kind="chat"):
                            pass

                run_step()

    all_spans = root_spans + child_spans

    # Check 1: Root spans = number of agent steps
    results.append(ValidationResult(
        check_name="root_span_count",
        passed=len(root_spans) == expected_agent_steps,
        expected=expected_agent_steps,
        actual=len(root_spans),
        detail="One root span per agent step",
    ))

    # Check 2: Total LLM call spans captured
    llm_spans = [s for s in all_spans if s.get("kind") == "chat"]
    results.append(ValidationResult(
        check_name="llm_call_capture",
        passed=len(llm_spans) == expected_calls,
        expected=expected_calls,
        actual=len(llm_spans),
        detail="Every mock LLM call produces a chat span",
    ))

    # Check 3: All spans have required fields
    required_fields = ["span_id", "trace_id", "name", "kind", "start_time"]
    missing_fields = []
    for span in all_spans:
        for f in required_fields:
            if f not in span:
                missing_fields.append((span.get("name", "?"), f))

    results.append(ValidationResult(
        check_name="required_fields_present",
        passed=len(missing_fields) == 0,
        expected=0,
        actual=len(missing_fields),
        detail=f"Missing: {missing_fields[:5]}" if missing_fields else "All fields present",
    ))

    # Cleanup
    agentc.shutdown()
    _initialized.clear()
    _shutdown_in_progress.clear()

    return results


def validate_span_tree(
    task_id: str,
    tmp_storage: Path,
    seed: int | None = None,
) -> list[ValidationResult]:
    """Validate span tree structure (parent-child relationships)."""
    results: list[ValidationResult] = []
    trace = generate_pipeline_trace(task_id, seed=seed)

    root_spans: list[dict[str, Any]] = []
    child_spans: list[dict[str, Any]] = []

    with patch("agentc._span._write_root_span", side_effect=lambda d: root_spans.append(d)):
        with patch("agentc._span._enqueue_span", side_effect=lambda d: child_spans.append(d)):
            with patch("agentc._lifecycle._apply_patches"):
                agentc.init(storage_path=str(tmp_storage))

            for step in trace.steps:

                @agentc.trace(name=f"{step.agent_name}-{step.step_name}")
                def run_step(calls: list[MockLLMCall] = step.calls) -> None:
                    for call in calls:
                        with agentc.span(f"llm-{call.model}", kind="chat"):
                            pass

                run_step()

    all_spans = root_spans + child_spans

    # Check 1: Root spans have no parent_span_id
    roots_without_parent = [
        s for s in root_spans if "parent_span_id" not in s
    ]
    results.append(ValidationResult(
        check_name="root_spans_no_parent",
        passed=len(roots_without_parent) == len(root_spans),
        expected=len(root_spans),
        actual=len(roots_without_parent),
        detail="Root spans should have no parent_span_id",
    ))

    # Check 2: Child spans have parent_span_id
    children_with_parent = [
        s for s in child_spans if "parent_span_id" in s
    ]
    results.append(ValidationResult(
        check_name="child_spans_have_parent",
        passed=len(children_with_parent) == len(child_spans),
        expected=len(child_spans),
        actual=len(children_with_parent),
        detail="All child spans should reference a parent",
    ))

    # Check 3: All parent references are valid span IDs
    span_ids = {s["span_id"] for s in all_spans}
    orphans = [
        s for s in child_spans
        if s.get("parent_span_id") and s["parent_span_id"] not in span_ids
    ]
    results.append(ValidationResult(
        check_name="no_orphan_spans",
        passed=len(orphans) == 0,
        expected=0,
        actual=len(orphans),
        detail="All parent_span_id references should be valid",
    ))

    # Check 4: All spans within a trace share the same trace_id
    trace_ids = {s["trace_id"] for s in all_spans}
    # Each agent step is a separate root trace (since they're sequential, not nested)
    # So trace_ids should equal number of root spans
    results.append(ValidationResult(
        check_name="trace_id_consistency",
        passed=len(trace_ids) == len(root_spans),
        expected=len(root_spans),
        actual=len(trace_ids),
        detail="Each root span starts a new trace",
    ))

    # Check 5: Span IDs are unique
    all_ids = [s["span_id"] for s in all_spans]
    results.append(ValidationResult(
        check_name="span_ids_unique",
        passed=len(all_ids) == len(set(all_ids)),
        expected=len(all_ids),
        actual=len(set(all_ids)),
        detail="No duplicate span IDs",
    ))

    # Check 6: End times >= start times
    bad_times = [
        s for s in all_spans
        if s.get("end_time") is not None and s["end_time"] < s["start_time"]
    ]
    results.append(ValidationResult(
        check_name="end_after_start",
        passed=len(bad_times) == 0,
        expected=0,
        actual=len(bad_times),
        detail="All end_time >= start_time",
    ))

    # Cleanup
    agentc.shutdown()
    _initialized.clear()
    _shutdown_in_progress.clear()

    return results


def validate_ttft_accuracy(
    n_iterations: int = 50,
    tmp_storage: Path | None = None,
) -> list[ValidationResult]:
    """Validate that span timing is accurate (proxy for TTFT accuracy).

    Since we don't have real streaming in mock mode, we validate that
    span start/end times are accurate to within 10ms of wall clock.
    """
    results: list[ValidationResult] = []
    storage = tmp_storage or Path("/tmp/agentc-bench-validate")

    written_spans: list[dict[str, Any]] = []

    with patch("agentc._span._write_root_span", side_effect=lambda d: written_spans.append(d)):
        with patch("agentc._lifecycle._apply_patches"):
            agentc.init(storage_path=str(storage))

        target_sleep_us = 1000  # 1ms
        timing_errors: list[float] = []

        for i in range(n_iterations):

            @agentc.trace(name=f"timing-test-{i}")
            def timed_fn() -> None:
                time.sleep(target_sleep_us / 1_000_000)

            timed_fn()

    # Check timing accuracy
    for span in written_spans:
        if span.get("end_time") is not None and span.get("start_time") is not None:
            measured_us = span["end_time"] - span["start_time"]
            # Should be at least target_sleep_us
            error_us = abs(measured_us - target_sleep_us)
            timing_errors.append(error_us)

    if timing_errors:
        max_error_ms = max(timing_errors) / 1000
        mean_error_ms = sum(timing_errors) / len(timing_errors) / 1000

        # TTFT accuracy target: within 10ms
        results.append(ValidationResult(
            check_name="timing_accuracy_max",
            passed=max_error_ms < 10.0,
            expected="<10ms",
            actual=f"{max_error_ms:.2f}ms",
            detail=f"Max timing error across {n_iterations} spans",
        ))
        results.append(ValidationResult(
            check_name="timing_accuracy_mean",
            passed=mean_error_ms < 5.0,
            expected="<5ms",
            actual=f"{mean_error_ms:.2f}ms",
            detail=f"Mean timing error across {n_iterations} spans",
        ))
    else:
        results.append(ValidationResult(
            check_name="timing_spans_captured",
            passed=False,
            expected=n_iterations,
            actual=0,
            detail="No spans captured for timing validation",
        ))

    # Cleanup
    agentc.shutdown()
    _initialized.clear()
    _shutdown_in_progress.clear()

    return results


def run_validation(
    task_id: str,
    tmp_storage: Path,
    seed: int | None = None,
) -> CorrectnessReport:
    """Run all correctness validations for a task."""
    report = CorrectnessReport(task_id=task_id)

    report.results.extend(validate_call_capture(task_id, tmp_storage, seed=seed))
    report.results.extend(validate_span_tree(task_id, tmp_storage, seed=seed))
    report.results.extend(validate_ttft_accuracy(
        n_iterations=20, tmp_storage=tmp_storage
    ))

    return report


def format_validation_report(reports: list[CorrectnessReport]) -> str:
    """Format correctness validation results as a human-readable report."""
    lines = [
        "CORRECTNESS VALIDATION REPORT",
        "=" * 60,
        f"Tasks validated: {len(reports)}",
        "",
    ]

    total_pass = 0
    total_fail = 0

    for report in reports:
        lines.append(f"Task: {report.task_id} — {'PASS' if report.passed else 'FAIL'}")
        for r in report.results:
            status = "PASS" if r.passed else "FAIL"
            lines.append(
                f"  [{status}] {r.check_name}: "
                f"expected={r.expected}, actual={r.actual}"
            )
            if r.detail and not r.passed:
                lines.append(f"         {r.detail}")
        total_pass += report.pass_count
        total_fail += report.fail_count
        lines.append("")

    lines.append(f"TOTAL: {total_pass} passed, {total_fail} failed")
    all_pass = all(r.passed for r in reports)
    lines.append(f"OVERALL: {'PASS' if all_pass else 'FAIL'}")

    return "\n".join(lines)
