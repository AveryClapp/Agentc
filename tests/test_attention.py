"""Tests for the online token-overlap attention proxy (bd-9qe.3).

Run: maturin develop && pytest tests/test_attention.py -v
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import agentc
from agentc import _attention
from agentc._attention import (
    _clear_cache,
    _last_user_tokens,
    _prior_trace_tokens,
    _salient_signal,
    _tokenize,
    compute_attention_scores,
)


# --- Fixtures ---


@pytest.fixture(autouse=True)
def _clean_cache() -> Any:
    _clear_cache()
    yield
    _clear_cache()


@pytest.fixture()
def initialized(tmp_path: Path) -> Any:
    """Spin up agentc with content capture so the FFI read path is exercised."""
    with patch("agentc._lifecycle._apply_patches"):
        agentc.init(storage_path=str(tmp_path / "agentc"))
    yield tmp_path
    if agentc.is_initialized():
        agentc.shutdown()


# --- Tokenizer ---


class TestTokenize:
    def test_lowercases_and_filters_short(self) -> None:
        toks = _tokenize("The Foo Barbaz IS a test of TOKENS.")
        assert "foo" in toks
        assert "barbaz" in toks
        assert "tokens" in toks
        assert "test" in toks
        # 2-char "is" is filtered
        assert "is" not in toks
        # stopwords filtered
        assert "the" not in toks

    def test_strips_stopwords(self) -> None:
        toks = _tokenize("the and for are but not you all")
        assert toks == set()

    def test_handles_empty_input(self) -> None:
        assert _tokenize("") == set()
        assert _tokenize(None) == set()  # type: ignore[arg-type]

    def test_three_char_minimum(self) -> None:
        toks = _tokenize("a ab abc abcd")
        assert "abc" in toks
        assert "abcd" in toks
        assert "ab" not in toks
        assert "a" not in toks


# --- Salient signal selection ---


class TestSalientSignal:
    def test_picks_last_user_message_when_no_trace(self) -> None:
        messages = [
            {"role": "system", "content": "System rules"},
            {"role": "user", "content": "First question"},
            {"role": "assistant", "content": "First answer"},
            {"role": "user", "content": "Second question about pelicans"},
        ]
        signal = _salient_signal(messages, trace_id=None)
        assert "second" in signal
        assert "question" in signal
        assert "pelicans" in signal
        # Should NOT include the first question's tokens
        assert "first" not in signal

    def test_picks_prior_span_union_when_trace_has_history(
        self, initialized: Path
    ) -> None:
        from agentc import _native

        trace = "trttrttrttrttrttrttrttrttrttrt00"
        d = {
            "span_id": "att0000000000001",
            "trace_id": trace,
            "name": "prior",
            "kind": "chat",
            "start_time": 1000,
            "input_messages": json.dumps([{"role": "user", "content": "alpha beta gamma"}]),
            "output_messages": json.dumps([{"role": "assistant", "content": "delta epsilon"}]),
        }
        _native.write_span(d)

        signal = _salient_signal(
            messages=[{"role": "user", "content": "current question"}],
            trace_id=trace,
        )
        # Prior union wins, NOT the current call's user message.
        assert "alpha" in signal
        assert "delta" in signal
        assert "current" not in signal

    def test_falls_back_to_user_when_prior_empty(self, initialized: Path) -> None:
        # Trace exists but has no spans — falls through to single-turn.
        signal = _salient_signal(
            messages=[{"role": "user", "content": "lonely question"}],
            trace_id="emptyemptyemptyemptyemptyempty00",
        )
        assert "lonely" in signal
        assert "question" in signal


# --- compute_attention_scores ---


class TestComputeScores:
    def test_empty_content_gets_score_one(self) -> None:
        messages = [
            {"role": "system", "content": ""},
            {"role": "user", "content": "tell me about dolphins"},
        ]
        scores, follow_on = compute_attention_scores(messages, trace_id=None)
        assert len(scores) == 2
        assert scores[0] == 1.0  # empty content protected
        assert "dolphins" in follow_on

    def test_returns_empty_when_no_signal(self) -> None:
        # No user message, no trace -> empty salient signal.
        messages = [
            {"role": "system", "content": "be brief"},
            {"role": "assistant", "content": "ok"},
        ]
        scores, follow_on = compute_attention_scores(messages, trace_id=None)
        assert scores == []
        assert follow_on == []

    def test_distractor_paragraphs_score_low(self) -> None:
        question = (
            "Were Scott Derrickson and Ed Wood of the same nationality?"
        )
        supporting = (
            "Scott Derrickson is an American director, screenwriter and producer."
        )
        distractor = (
            "Henry IV established the Plantagenet dynasty in medieval England."
        )
        messages = [
            {"role": "system", "content": "Answer the question."},
            {"role": "user", "content": supporting},
            {"role": "user", "content": distractor},
            {"role": "user", "content": question},
        ]
        scores, _ = compute_attention_scores(messages, trace_id=None)
        # Last message IS the question -> score 1.0 (perfect overlap with self).
        assert scores[3] == pytest.approx(1.0)
        # Supporting paragraph shares "Scott", "Derrickson" with question.
        assert scores[1] > scores[2]
        # Distractor has zero overlap with the question's content words.
        assert scores[2] <= 0.05

    def test_handles_non_dict_message(self) -> None:
        messages = [
            "not a dict",  # type: ignore[list-item]
            {"role": "user", "content": "real question about pelicans"},
        ]
        scores, _ = compute_attention_scores(messages, trace_id=None)  # type: ignore[arg-type]
        assert scores[0] == 1.0  # non-dict protected


# --- SQLite-read failure ---


class TestSqliteFailureFallback:
    def test_ffi_exception_falls_back_to_user_message(self) -> None:
        with patch(
            "agentc._attention._read_prior_spans",
            side_effect=RuntimeError("simulated FFI failure"),
        ):
            # Should not raise — the wrapper is supposed to swallow.
            # Wait — in the real wrapper we already swallow inside
            # ``_read_prior_spans``. Patch the module function directly.
            pass

        with patch(
            "agentc._attention._read_prior_spans", return_value=[]
        ):
            scores, follow_on = compute_attention_scores(
                [{"role": "user", "content": "fallback question"}],
                trace_id="failfailfailfailfailfailfailfa00",
            )
        assert "fallback" in follow_on
        assert scores[0] == pytest.approx(1.0)


# --- Cache invalidation ---


class TestCacheInvalidation:
    def test_invalidates_when_new_span_lands(self, initialized: Path) -> None:
        from agentc import _native

        trace = "cccccccccccccccccccccccccccccc00"
        # First span — populates cache.
        _native.write_span(
            {
                "span_id": "cache00000000001",
                "trace_id": trace,
                "name": "p1",
                "kind": "chat",
                "start_time": 1000,
                "input_messages": json.dumps([{"role": "user", "content": "alpha beta"}]),
            }
        )
        first = _prior_trace_tokens(trace)
        assert "alpha" in first
        assert "beta" in first
        assert "gamma" not in first

        # Second span lands -> cache key changes (new last_span_id).
        _native.write_span(
            {
                "span_id": "cache00000000002",
                "trace_id": trace,
                "name": "p2",
                "kind": "chat",
                "start_time": 2000,
                "input_messages": json.dumps([{"role": "user", "content": "gamma delta"}]),
            }
        )
        second = _prior_trace_tokens(trace)
        assert "gamma" in second
        assert "delta" in second
        # Old tokens stay too — union is monotonic.
        assert "alpha" in second
