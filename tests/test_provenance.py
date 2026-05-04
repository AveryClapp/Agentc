"""Unit tests for ``agentc._provenance`` — object-provenance tagger.

Covers the happy path (tag/retrieve round-trip per DepSource variant),
the raw-SDK fallback (untagged object → PROVENANCE_UNSET serializing as
literal), and the bounded primitive-tag fallback for weak-ref-hostile
types like ``str``.
"""

from __future__ import annotations

import json

import pytest

from agentc._provenance import (
    PROVENANCE_UNSET,
    Literal,
    LlmOutput,
    State,
    ToolOutput,
    UserInput,
    as_json,
    clear,
    consume_state_reads,
    record_state_read,
    state_read,
    state_write,
    tag,
    tag_of,
)


@pytest.fixture(autouse=True)
def _reset_tags():
    clear()
    yield
    clear()


def test_untagged_object_is_unset():
    obj = {"content": "hi"}
    assert tag_of(obj) is PROVENANCE_UNSET
    # Unset serializes to literal so the Rust FFI sees a valid DepSource.
    assert as_json(tag_of(obj)) == {"kind": "literal"}


def test_roundtrip_user_input():
    obj = {"content": "from root"}
    tag(obj, UserInput(span_id="deadbeefdeadbeef"))
    retrieved = tag_of(obj)
    assert isinstance(retrieved, UserInput)
    assert retrieved.span_id == "deadbeefdeadbeef"
    assert as_json(retrieved) == {
        "kind": "user_input",
        "span_id": "deadbeefdeadbeef",
    }


def test_roundtrip_tool_and_llm_output():
    tool_obj = {"content": "tool result"}
    llm_obj = {"content": "llm response"}
    tag(tool_obj, ToolOutput(span_id="t" * 16))
    tag(llm_obj, LlmOutput(span_id="l" * 16))
    assert as_json(tag_of(tool_obj)) == {
        "kind": "tool_output",
        "span_id": "t" * 16,
    }
    assert as_json(tag_of(llm_obj)) == {
        "kind": "llm_output",
        "span_id": "l" * 16,
    }


def test_state_tag_carries_key():
    obj = {"content": "memory"}
    tag(obj, State(key="plan_memory"))
    js = as_json(tag_of(obj))
    assert js == {"kind": "state", "key": "plan_memory"}


def test_retag_overwrites():
    obj = {"content": "x"}
    tag(obj, UserInput(span_id="a" * 16))
    tag(obj, ToolOutput(span_id="b" * 16))
    t = tag_of(obj)
    assert isinstance(t, ToolOutput)
    assert t.span_id == "b" * 16


def test_returns_original_object():
    obj = {"content": "piped"}
    returned = tag(obj, Literal())
    assert returned is obj


def test_primitive_fallback_for_strings():
    # str can't be weakly referenced; the bounded dict path handles it.
    s = "primitive content " + "x" * 32  # avoid interning small string
    tag(s, UserInput(span_id="c" * 16))
    t = tag_of(s)
    assert isinstance(t, UserInput)
    assert t.span_id == "c" * 16


def test_raw_sdk_fallback_produces_literal_everywhere():
    # Messages constructed without any tagging — the raw-SDK fallback
    # the spec promises. Every retrieved tag is literal → ParallelBranch
    # / StateDrop no-op but everything still serializes cleanly.
    messages = [{"role": "user", "content": "hi"}, {"role": "system", "content": "sys"}]
    tags = [as_json(tag_of(m)) for m in messages]
    assert all(t == {"kind": "literal"} for t in tags)


def test_as_json_handles_none():
    assert as_json(None) == {"kind": "literal"}


def test_state_window_records_and_consumes():
    record_state_read("notes")
    record_state_read("plan")
    record_state_read("notes")  # dedupe
    snap = consume_state_reads()
    assert snap == ["notes", "plan"]
    # consume clears.
    assert consume_state_reads() == []


def test_state_write_tags_but_does_not_record():
    notes = "abc-research-notes-content-xxxxx"  # avoid interning
    returned = state_write("notes", notes)
    assert returned is notes
    assert as_json(tag_of(notes)) == {"kind": "state", "key": "notes"}
    # write must NOT enter the read window.
    assert consume_state_reads() == []


def test_state_read_records_and_tags():
    critique = "abc-critique-content-xxxxx"
    returned = state_read("critique", critique)
    assert returned is critique
    assert as_json(tag_of(critique)) == {"kind": "state", "key": "critique"}
    assert consume_state_reads() == ["critique"]


def test_clear_also_clears_state_window():
    record_state_read("k1")
    record_state_read("k2")
    clear()
    assert consume_state_reads() == []


def test_state_window_is_thread_local():
    import threading

    record_state_read("main-only")
    other_window: list[list[str]] = []

    def worker() -> None:
        # Worker thread starts with an empty window.
        other_window.append(consume_state_reads())
        record_state_read("worker-only")
        other_window.append(consume_state_reads())

    t = threading.Thread(target=worker)
    t.start()
    t.join()

    assert other_window[0] == []  # fresh in worker
    assert other_window[1] == ["worker-only"]
    # Main thread's window untouched by the worker.
    assert consume_state_reads() == ["main-only"]


def test_as_json_emits_valid_json():
    # Make sure every variant round-trips through json.dumps — the Rust
    # side is the authoritative deserializer.
    for src in [
        Literal(),
        UserInput(span_id="1234abcd1234abcd"),
        ToolOutput(span_id="abcdef0123456789"),
        LlmOutput(span_id="0123456789abcdef"),
        State(key="plan_memory"),
    ]:
        encoded = json.dumps(as_json(src))
        decoded = json.loads(encoded)
        assert "kind" in decoded
