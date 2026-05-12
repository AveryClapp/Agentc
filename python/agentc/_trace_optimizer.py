"""TraceOptimizer — cross-call inference passes over a sliding trace window.

Three passes run after each call completes:

1. StateReadWindowPropagation: infer state reads from LlmOutput token
   overlap. Compiler analog: live variable analysis. Enables StateDrop
   to fire on uninstrumented agents by injecting inferred keys into
   ``parameters.extra.window_state_reads`` before the next call.

2. DeadOutputDetection: flag call sites whose output tokens never appear
   in any downstream input. Compiler analog: dead store elimination.
   Sets ``parameters.extra.output_is_dead_branch = true`` for
   DeadOutputTruncation to act on.

3. PrefixAlignDetection: detect shared message prefixes between
   consecutive calls. Compiler analog: prefix sharing / common header
   factoring. Sets ``parameters.extra.shared_prefix_messages`` for the
   PrefixAlign rule.

Recommendations are injected into the NEXT call's
``parameters.extra`` by the optimizer glue before ``plan_call`` sees
it, so all three rule firings are transparent to application code.
"""

from __future__ import annotations

import re
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional

DEFAULT_WINDOW = 16
MIN_TOKEN_LEN = 3
# Dead-output detection uses longer tokens to avoid false negatives from
# common English stopwords ("the", "and", "for") that appear in any two
# English texts and would otherwise always register as "alive".
MIN_TOKEN_LEN_DEAD = 6
MIN_PREFIX_BYTES = 2 * 1024


@dataclass
class CallRecord:
    trace_id: str
    span_id: str
    call_site_id: str
    model: str
    messages: list[dict[str, Any]]
    output_content: str
    input_deps: list[dict[str, Any]]
    fired_rules: list[str] = field(default_factory=list)


@dataclass
class TraceRecommendations:
    output_is_dead_branch: bool = False
    shared_prefix_messages: list[dict[str, Any]] = field(default_factory=list)
    inferred_state_reads: list[str] = field(default_factory=list)


def _tokenize(text: str) -> set[str]:
    return {w.lower() for w in re.split(r"\W+", text) if len(w) >= MIN_TOKEN_LEN}


class TraceOptimizer:
    def __init__(self, window: int = DEFAULT_WINDOW) -> None:
        self._window = window
        self._windows: dict[str, deque[CallRecord]] = {}
        self._recommendations: dict[str, TraceRecommendations] = {}
        self._lock = threading.Lock()

    def record(self, record: CallRecord) -> TraceRecommendations:
        with self._lock:
            if record.trace_id not in self._windows:
                self._windows[record.trace_id] = deque(maxlen=self._window)
            self._windows[record.trace_id].append(record)
            recs = self._run_passes(record.trace_id)
            self._recommendations[record.trace_id] = recs
            return recs

    def get_recommendations(self, trace_id: str) -> TraceRecommendations:
        with self._lock:
            return self._recommendations.get(trace_id, TraceRecommendations())

    def invalidate(self, trace_id: str) -> None:
        with self._lock:
            self._windows.pop(trace_id, None)
            self._recommendations.pop(trace_id, None)

    def _run_passes(self, trace_id: str) -> TraceRecommendations:
        window = list(self._windows[trace_id])
        recs = TraceRecommendations()
        _pass_state_read_propagation(window, recs)
        _pass_dead_output_detection(window, recs)
        _pass_prefix_align_detection(window, recs)
        return recs


def _pass_state_read_propagation(
    window: list[CallRecord], recs: TraceRecommendations
) -> None:
    """Live variable analysis: if a state key's tokens appear in any
    LlmOutput in the window, infer that key was read by the agent."""
    if not window:
        return
    all_output_tokens: set[str] = set()
    for record in window:
        all_output_tokens |= _tokenize(record.output_content)

    for record in window:
        for msg in record.messages:
            dep = msg.get("__dep__")
            if not dep:
                continue
            kind = dep.get("kind", "")
            if kind != "state":
                continue
            key = dep.get("key", "")
            if not key:
                continue
            key_tokens = _tokenize(key)
            if key_tokens & all_output_tokens and key not in recs.inferred_state_reads:
                recs.inferred_state_reads.append(key)


def _tokenize_dead(text: str) -> set[str]:
    """Tokenizer for dead-output detection: uses MIN_TOKEN_LEN_DEAD (6) to
    filter stopwords ("the", "and", "for") that appear in any English text
    and would otherwise produce spurious overlap between unrelated documents."""
    return {w.lower() for w in re.split(r"\W+", text) if len(w) >= MIN_TOKEN_LEN_DEAD}


def _pass_dead_output_detection(
    window: list[CallRecord], recs: TraceRecommendations
) -> None:
    """Dead store elimination: the previous call's output is dead if its
    distinctive tokens never appear in any subsequent call's inputs."""
    if len(window) < 2:
        return
    prev = window[-2]
    if not prev.output_content:
        return
    prev_tokens = _tokenize_dead(prev.output_content)
    if len(prev_tokens) < 3:
        return
    prev_idx = len(window) - 2
    for record in window[prev_idx + 1 :]:
        input_tokens: set[str] = set()
        for msg in record.messages:
            input_tokens |= _tokenize_dead(msg.get("content", ""))
        if prev_tokens & input_tokens:
            return
    recs.output_is_dead_branch = True


def _pass_prefix_align_detection(
    window: list[CallRecord], recs: TraceRecommendations
) -> None:
    """Detect shared message prefix between consecutive calls for PrefixAlign."""
    if len(window) < 2:
        return
    prev_msgs = window[-2].messages
    curr_msgs = window[-1].messages
    prefix: list[dict[str, Any]] = []
    for pm, cm in zip(prev_msgs, curr_msgs):
        if pm.get("role") == cm.get("role") and pm.get("content") == cm.get("content"):
            prefix.append(dict(cm))
        else:
            break
    prefix_bytes = sum(len(m.get("content", "")) for m in prefix)
    if prefix_bytes >= MIN_PREFIX_BYTES:
        recs.shared_prefix_messages = prefix


_global: Optional[TraceOptimizer] = None


def get_trace_optimizer() -> Optional[TraceOptimizer]:
    return _global


def init_trace_optimizer(window: int = DEFAULT_WINDOW) -> TraceOptimizer:
    global _global
    _global = TraceOptimizer(window=window)
    return _global
