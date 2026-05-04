"""Unit tests for ``agentc._patches._optimizer_glue.build_call_dict_openai``.

The OpenAI patch is the only place where StateDrop / ContextCompress
get their per-message provenance and window-state-reads. These tests
pin the contract:

- ``input_deps`` is mirrored as ``parameters.extra.message_deps``.
- The state-read window is consumed and surfaced as
  ``parameters.extra.window_state_reads``.
- Untagged messages serialize as ``literal``.
"""

from __future__ import annotations

import pytest

from agentc._patches._optimizer_glue import build_call_dict_openai
from agentc._provenance import (
    State,
    UserInput,
    clear,
    consume_state_reads,
    record_state_read,
    state_read,
    state_write,
    tag,
)


@pytest.fixture(autouse=True)
def _reset_provenance():
    clear()
    yield
    clear()


def _build(messages: list[dict]) -> dict:
    return build_call_dict_openai(
        {"model": "gpt-4o-mini", "messages": messages},
        call_site_id="test:site:1",
        trace_id_hex="00" * 16,
        span_id_hex="00" * 8,
    )


def test_message_deps_mirrors_input_deps_for_untagged():
    msgs = [
        {"role": "system", "content": "sys-prompt-content-xxxxx"},
        {"role": "user", "content": "user-prompt-content-xxxxx"},
    ]
    call = _build(msgs)
    assert call["input_deps"] == [{"kind": "literal"}, {"kind": "literal"}]
    assert call["parameters"]["extra"]["message_deps"] == [
        {"kind": "literal"},
        {"kind": "literal"},
    ]


def test_state_tag_appears_in_message_deps():
    notes = "research-notes-content-xxxxx"
    state_write("notes", notes)
    msgs = [{"role": "user", "content": notes}]
    call = _build(msgs)
    assert call["parameters"]["extra"]["message_deps"] == [
        {"kind": "state", "key": "notes"}
    ]


def test_user_input_tag_appears_in_message_deps():
    prompt = "user-prompt-content-xxxxx"
    tag(prompt, UserInput(span_id="a" * 16))
    msgs = [{"role": "user", "content": prompt}]
    call = _build(msgs)
    assert call["parameters"]["extra"]["message_deps"] == [
        {"kind": "user_input", "span_id": "a" * 16}
    ]


def test_window_state_reads_is_consumed_and_cleared():
    record_state_read("notes")
    record_state_read("plan")
    msgs = [{"role": "user", "content": "anything-content-xxxxx"}]
    call = _build(msgs)
    assert call["parameters"]["extra"]["window_state_reads"] == ["notes", "plan"]
    # The next build should see an empty window — consume cleared it.
    call2 = _build(msgs)
    assert call2["parameters"]["extra"]["window_state_reads"] == []


def test_attention_scores_populated_for_hotpot_shape():
    """ContextCompress reads attention_scores + follow_on_tokens. Verify
    a HotpotQA-shaped call (system + paragraphs + question) produces
    them via the single-turn fallback in ``_attention``."""
    msgs = [
        {"role": "system", "content": "Answer the question briefly."},
        {
            "role": "user",
            "content": "Scott Derrickson is an American director and screenwriter.",
        },
        {
            "role": "user",
            "content": "Henry IV established the Plantagenet dynasty in medieval England.",
        },
        {
            "role": "user",
            "content": "Were Scott Derrickson and Ed Wood of the same nationality?",
        },
    ]
    call = _build(msgs)
    extra = call["parameters"]["extra"]
    assert "attention_scores" in extra
    assert len(extra["attention_scores"]) == len(msgs)
    assert "follow_on_tokens" in extra
    # Question's own tokens are in the follow_on set.
    assert "scott" in extra["follow_on_tokens"]
    assert "derrickson" in extra["follow_on_tokens"]
    # Distractor (Henry IV) has near-zero overlap with the question.
    assert extra["attention_scores"][2] <= 0.05
    # The question itself overlaps perfectly with itself.
    assert extra["attention_scores"][3] == pytest.approx(1.0)


def test_attention_scores_omitted_when_no_signal():
    """When the call has no user message and no trace history, the proxy
    returns ``([], [])`` and the glue must not include the keys at all
    (the rule will refuse to fire on length-mismatched input)."""
    msgs = [
        {"role": "system", "content": "be brief"},
        {"role": "assistant", "content": "ok"},
    ]
    call = _build(msgs)
    extra = call["parameters"]["extra"]
    assert "attention_scores" not in extra
    assert "follow_on_tokens" not in extra


def test_state_drop_payload_shape_matches_rule_contract():
    """Round-trip a refiner-shaped call. State("notes") is in messages
    but only "critique" is in the read window — the Rust rule should be
    able to identify "notes" as drop-eligible."""
    notes = "research-notes-content-xxxxx"
    critique = "critique-content-xxxxx"
    final = "final-prompt-content-xxxxx"
    state_write("notes", notes)
    state_write("critique", critique)
    # Simulate the agent reading critique just before this LLM call.
    state_read("critique", critique)

    msgs = [
        {"role": "system", "content": "sys-content-xxxxx"},
        {"role": "user", "content": notes},
        {"role": "user", "content": critique},
        {"role": "user", "content": final},
    ]
    call = _build(msgs)
    extra = call["parameters"]["extra"]
    assert extra["message_deps"] == [
        {"kind": "literal"},
        {"kind": "state", "key": "notes"},
        {"kind": "state", "key": "critique"},
        {"kind": "literal"},
    ]
    assert extra["window_state_reads"] == ["critique"]
    # consume_state_reads must have cleared the window.
    assert consume_state_reads() == []
