"""End-to-end checks for ``agentc.parallel_map`` and the
``ParallelBranch`` rule.

What's covered:
- ``parallel_map`` preserves input order and returns ``fn(item_i)`` at
  position i.
- The peer thread-local is staged during the worker call and cleared
  after ``parallel_map`` returns.
- ``build_call_dict_openai`` reads the per-message ``DepSource`` tag
  via ``_provenance.tag_of`` and the staged peer via the parallel
  helper, so a Call dict reaching the rule engine has the shape
  ``ParallelBranchRule`` requires.
- A warmed-up cost model + a peer-staged Call yields a
  ``Plan::Parallel`` proposal carrying ``rule = ParallelBranch``.

Real OpenAI traffic is mocked: we never reach the network.
"""

from __future__ import annotations

import os
import tempfile

import pytest


@pytest.fixture
def storage(monkeypatch):
    """Per-test storage dir so audit DBs from other tests don't bleed in."""
    d = tempfile.mkdtemp(prefix="agentc-parallel-test-")
    monkeypatch.setenv("AGENTC_STORAGE_PATH", d)
    yield d


def test_parallel_map_preserves_order_and_clears_peer():
    import agentc
    from agentc._parallel import get_parallel_peer

    seen: list[dict | None] = []

    def _capture(x: str) -> str:
        seen.append(get_parallel_peer())
        return x.upper()

    out = agentc.parallel_map(_capture, ["a", "b", "c"])
    assert out == ["A", "B", "C"]
    # Each worker saw a peer descriptor while running.
    assert all(p is not None for p in seen)
    # Peers contain disjoint, concrete user_input span ids.
    span_ids = [p["input_deps"][0]["span_id"] for p in seen]
    assert len(set(span_ids)) == len(span_ids)
    # Thread-local is cleared on return.
    assert get_parallel_peer() is None


def test_parallel_map_singleton_degrades_serial():
    """One item: nothing to pair with — serial fallback, no peer."""
    import agentc
    from agentc._parallel import get_parallel_peer

    seen: list[dict | None] = []
    out = agentc.parallel_map(lambda x: (seen.append(get_parallel_peer()) or x), ["only"])
    assert out == ["only"]
    assert seen == [None]


def test_build_call_dict_threads_tag_and_peer():
    """The OpenAI glue must read both the message-level provenance tag
    and the thread-local peer descriptor."""
    from agentc._parallel import _set_peer
    from agentc._patches._optimizer_glue import build_call_dict_openai
    from agentc._provenance import UserInput, tag

    prompt = "summarize this please"
    tag(prompt, UserInput(span_id="cafefacecafeface"))
    kwargs = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": "be concise"},
            {"role": "user", "content": prompt},
        ],
    }

    # No peer: input_deps populated, parameters.extra carries the
    # StateDrop / ContextCompress contract (mirrored message_deps and an
    # empty window_state_reads), but no parallel_peer.
    d = build_call_dict_openai(
        kwargs, call_site_id="cs", trace_id_hex="0" * 32, span_id_hex="0" * 16
    )
    assert d["input_deps"] == [
        {"kind": "literal"},
        {"kind": "user_input", "span_id": "cafefacecafeface"},
    ]
    extra = d["parameters"]["extra"]
    assert "parallel_peer" not in extra
    assert extra["message_deps"] == d["input_deps"]
    assert extra["window_state_reads"] == []

    # With peer staged: parameters.extra.parallel_peer present.
    _set_peer({"input_deps": [{"kind": "user_input", "span_id": "deadbeefdeadbeef"}]})
    try:
        d = build_call_dict_openai(
            kwargs, call_site_id="cs", trace_id_hex="0" * 32, span_id_hex="0" * 16
        )
        assert d["parameters"]["extra"]["parallel_peer"]["input_deps"][0]["kind"] == "user_input"
    finally:
        _set_peer(None)


def test_parallel_branch_fires_and_audits(storage):
    """End-to-end: warm cost model, then a peer-staged Call dict
    produces a ``Plan::Parallel`` proposal with ``rule=ParallelBranch``,
    and the corresponding audit row lands in ``optimizer_audit.db``.

    Combined into one test because ``agentc.init()`` reads the storage
    path on first init and doesn't fully reset on subsequent
    init/shutdown cycles within the same Python process — splitting this
    across two tests would require subprocess isolation which isn't
    worth the complexity for a single rule-firing assertion."""
    import sqlite3

    import agentc

    agentc.init()
    try:
        from agentc._optimizer import observe_outcome, plan_call

        warm_call = {
            "call_site_id": "cs.test_pb",
            "trace_id": "0" * 32,
            "span_id": "0" * 16,
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "warm"}],
            "parameters": {},
            "tools": [],
            "input_deps": [{"kind": "user_input", "span_id": "deadbeefdeadbeef"}],
            "occurrence_ix": 0,
        }
        warm_out = {
            "input_tokens": 10,
            "output_tokens": 5,
            "latency_ms": 100.0,
            "cost_usd": 0.0001,
            "output_is_structured": False,
            "output_is_short": True,
            "call_site_id": "cs.test_pb",
        }
        for _ in range(5):
            observe_outcome(plan_call(warm_call), warm_out)

        peer_call = dict(warm_call)
        peer_call["parameters"] = {
            "extra": {
                "parallel_peer": {
                    "input_deps": [
                        {"kind": "user_input", "span_id": "abababababababab"},
                    ]
                }
            }
        }
        plan = plan_call(peer_call)
        assert plan.kind == "parallel"
        assert getattr(plan, "rule", None) == "ParallelBranch"
        assert len(getattr(plan, "calls", [])) == 2
        observe_outcome(plan, warm_out)
    finally:
        agentc.shutdown()

    db = os.path.join(storage, "optimizer_audit.db")
    assert os.path.exists(db), "audit DB should exist after shutdown"
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "select rule, plan_kind from plan_audit where rule = 'ParallelBranch'"
    ).fetchall()
    assert rows, "expected ≥1 ParallelBranch audit row"
    assert all(r[1] == "parallel" for r in rows)
