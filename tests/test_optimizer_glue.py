"""Unit tests for ``agentc._patches._optimizer_glue``.

Covers:
- ``build_call_dict_openai``: StateDrop / ContextCompress provenance contract.
- ``_text_divergence``: lexical, normalized, and embedding divergence modes.
"""

from __future__ import annotations

import pytest

from agentc._patches._optimizer_glue import (
    _text_divergence,
    build_call_dict_openai,
    dispatch_sync,
)
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


# ---------------------------------------------------------------------------
# _text_divergence — embedding mode
# ---------------------------------------------------------------------------

class TestEmbeddingDivergenceMode:
    """Tests for AGENTC_SHADOW_DIVERGENCE_MODE=embedding."""

    def test_identical_strings_score_near_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENTC_SHADOW_DIVERGENCE_MODE", "embedding")
        score = _text_divergence("The answer is Paris.", "The answer is Paris.")
        assert score < 0.05, f"identical strings gave divergence {score:.4f}; expected < 0.05"

    def test_unrelated_strings_score_high(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENTC_SHADOW_DIVERGENCE_MODE", "embedding")
        score = _text_divergence(
            "The capital of France is Paris.",
            "Photosynthesis converts sunlight into chemical energy in plants.",
        )
        assert score > 0.2, f"unrelated strings gave low divergence {score:.4f}; expected > 0.2"

    def test_paraphrase_lower_than_raw_lexical(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The motivating case: 'The answer is Paris' vs 'Paris is the answer'
        should score LOWER divergence in embedding mode than in lexical mode,
        because the embedding captures semantic equivalence."""
        a = "The answer is Paris."
        b = "Paris is the answer."

        monkeypatch.setenv("AGENTC_SHADOW_DIVERGENCE_MODE", "lexical")
        lexical_score = _text_divergence(a, b)

        monkeypatch.setenv("AGENTC_SHADOW_DIVERGENCE_MODE", "embedding")
        embedding_score = _text_divergence(a, b)

        assert embedding_score < lexical_score, (
            f"embedding ({embedding_score:.4f}) should be < lexical ({lexical_score:.4f}) "
            "for a semantically equivalent paraphrase"
        )

    def test_fallback_when_native_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When _native.embed_text_bytes raises, the mode falls back to
        'normalized' (containment) rather than crashing."""
        import unittest.mock

        monkeypatch.setenv("AGENTC_SHADOW_DIVERGENCE_MODE", "embedding")
        # Patch embed_text_bytes to raise so we exercise the fallback path.
        with unittest.mock.patch(
            "agentc._native.embed_text_bytes",
            side_effect=RuntimeError("embedder offline"),
        ):
            # Must not raise; should return a valid float via normalized fallback.
            score = _text_divergence("hello world", "hello world")
            assert isinstance(score, float)
            assert 0.0 <= score <= 1.0


class TestDispatchSyncCachedFallback:
    """Regression (bd-8ln): a cached plan whose payload cannot be decoded must
    fall back to the real call, never return None to the app while booking a
    cache hit."""

    @staticmethod
    def _plan(kind: str, value: object = None):
        from types import SimpleNamespace

        return SimpleNamespace(kind=kind, value=value, call=None, rule="R")

    def test_cached_decode_none_falls_back_to_original(self) -> None:
        out = dispatch_sync(
            self._plan("cached", value={"output_content_id": "x"}),
            run_original=lambda: "REAL_COMPLETION",
            run_mutated=lambda c: "MUT",
            decode_cached=lambda v: None,  # decoder returns None instead of raising
        )
        assert out == "REAL_COMPLETION"

    def test_cached_decode_raise_falls_back_to_original(self) -> None:
        def _boom(_v):
            raise RuntimeError("decode failed")

        out = dispatch_sync(
            self._plan("cached", value={"output_content_id": "x"}),
            run_original=lambda: "REAL",
            run_mutated=lambda c: "MUT",
            decode_cached=_boom,
        )
        assert out == "REAL"

    def test_cached_valid_value_is_returned(self) -> None:
        out = dispatch_sync(
            self._plan("cached", value={"output_content_id": "x"}),
            run_original=lambda: "REAL",
            run_mutated=lambda c: "MUT",
            decode_cached=lambda v: "DECODED",
        )
        assert out == "DECODED"

    def test_cached_falsy_but_valid_value_is_returned(self) -> None:
        # Only None triggers fallback — a legitimately cached 0/""/False stays.
        out = dispatch_sync(
            self._plan("cached", value={"output_content_id": "x"}),
            run_original=lambda: "REAL",
            run_mutated=lambda c: "MUT",
            decode_cached=lambda v: 0,
        )
        assert out == 0
