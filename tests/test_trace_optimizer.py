"""Tests for TraceOptimizer cross-call inference passes."""

from __future__ import annotations

import pytest
from agentc._trace_optimizer import CallRecord, TraceOptimizer, TraceRecommendations


def make_record(
    trace_id: str,
    span_id: str,
    site: str,
    messages: list,
    output: str,
    deps: list | None = None,
) -> CallRecord:
    return CallRecord(
        trace_id=trace_id,
        span_id=span_id,
        call_site_id=site,
        model="gpt-4o",
        messages=messages,
        output_content=output,
        input_deps=deps or [],
        fired_rules=[],
    )


def test_window_bounded_at_n() -> None:
    opt = TraceOptimizer(window=3)
    for i in range(5):
        opt.record(make_record("t", f"s{i}", "site", [], "output"))
    assert len(opt._windows["t"]) == 3


def test_state_read_propagation_infers_key_from_output_tokens() -> None:
    """If state key 'plan' tokens appear in LlmOutput, infer state_read('plan')."""
    opt = TraceOptimizer(window=8)
    r1 = make_record(
        "t",
        "s1",
        "site",
        messages=[
            {
                "role": "user",
                "content": "step one step two",
                "__dep__": {"kind": "state", "key": "plan"},
            }
        ],
        output="Based on the plan I will execute step one first.",
        deps=[{"kind": "state", "key": "plan"}],
    )
    recs = opt.record(r1)
    assert "plan" in recs.inferred_state_reads, (
        f"'plan' tokens appear in output; expected state read inference. "
        f"Got: {recs.inferred_state_reads}"
    )


def test_state_read_propagation_does_not_infer_absent_key() -> None:
    opt = TraceOptimizer(window=8)
    r1 = make_record(
        "t",
        "s1",
        "site",
        messages=[
            {
                "role": "user",
                "content": "task execution details",
                "__dep__": {"kind": "state", "key": "memory_store"},
            }
        ],
        output="Here is the answer to your question about France.",
    )
    recs = opt.record(r1)
    assert "memory_store" not in recs.inferred_state_reads


def test_dead_output_detection_flags_unreferenced_output() -> None:
    opt = TraceOptimizer(window=8)
    r1 = make_record(
        "t",
        "s1",
        "thinker",
        messages=[{"role": "user", "content": "think about this problem"}],
        output="UNIQUETOKEN reasoning intermediate step XYZZY",
    )
    r2 = make_record(
        "t",
        "s2",
        "responder",
        messages=[{"role": "user", "content": "unrelated followup question about Paris"}],
        output="the answer",
    )
    opt.record(r1)
    recs = opt.record(r2)
    assert recs.output_is_dead_branch, (
        "s1 output 'UNIQUETOKEN...XYZZY' not in s2 inputs → should be flagged as dead"
    )


def test_dead_output_not_flagged_when_referenced() -> None:
    opt = TraceOptimizer(window=8)
    r1 = make_record("t", "s1", "thinker", [], "result is forty two")
    r2 = make_record(
        "t",
        "s2",
        "responder",
        messages=[{"role": "user", "content": "the result is forty two as computed"}],
        output="yes",
    )
    opt.record(r1)
    recs = opt.record(r2)
    assert not recs.output_is_dead_branch


def test_prefix_align_detected_above_threshold() -> None:
    opt = TraceOptimizer(window=8)
    shared = "x" * 2048  # 2 KB
    r1 = make_record(
        "t",
        "s1",
        "site",
        messages=[
            {"role": "system", "content": shared},
            {"role": "user", "content": "first question"},
        ],
        output="answer one",
    )
    r2 = make_record(
        "t",
        "s2",
        "site",
        messages=[
            {"role": "system", "content": shared},
            {"role": "user", "content": "second question"},
        ],
        output="answer two",
    )
    opt.record(r1)
    recs = opt.record(r2)
    assert len(recs.shared_prefix_messages) == 1
    assert recs.shared_prefix_messages[0]["content"] == shared


def test_prefix_align_not_detected_below_threshold() -> None:
    opt = TraceOptimizer(window=8)
    r1 = make_record(
        "t", "s1", "site",
        messages=[{"role": "system", "content": "short"}],
        output="a",
    )
    r2 = make_record(
        "t", "s2", "site",
        messages=[{"role": "system", "content": "short"}],
        output="b",
    )
    opt.record(r1)
    recs = opt.record(r2)
    assert recs.shared_prefix_messages == []


def test_invalidate_clears_trace() -> None:
    opt = TraceOptimizer(window=8)
    opt.record(make_record("t", "s1", "site", [], "output"))
    opt.invalidate("t")
    assert "t" not in opt._windows
    assert opt.get_recommendations("t") == TraceRecommendations()


def test_separate_traces_are_isolated() -> None:
    opt = TraceOptimizer(window=4)
    opt.record(make_record("trace_a", "s1", "site", [], "UNIQUETOKEN for trace a"))
    opt.record(make_record("trace_b", "s1", "site", [], "completely different output"))
    opt.record(make_record("trace_b", "s2", "site",
        messages=[{"role": "user", "content": "unrelated question"}],
        output="b answer"))
    recs_b = opt.get_recommendations("trace_b")
    # Dead-output detection: trace_b's first output not referenced in trace_b's second input.
    assert recs_b.output_is_dead_branch
    # trace_a has only one record → no dead-output flag possible.
    recs_a = opt.get_recommendations("trace_a")
    assert not recs_a.output_is_dead_branch
