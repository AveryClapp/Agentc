"""Tests for Anthropic SDK patches (bd-2up).

Run: maturin develop && pytest tests/test_anthropic_patch.py -v
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import agentc
from agentc._context import SpanContext, get_current_span, set_current_span
from agentc._lifecycle import _initialized, _shutdown_in_progress
from agentc._patches._anthropic import (
    _extract_input_messages,
    _extract_request_attrs,
    _extract_response_attrs,
    _wrap_create,
    _wrap_stream,
    _WrappedStreamManager,
    _emit_span,
    patch as patch_anthropic,
    unpatch as unpatch_anthropic,
)


# --- Mock Anthropic response objects ---


class MockUsage:
    def __init__(
        self,
        input_tokens: int = 100,
        output_tokens: int = 50,
        cache_creation_input_tokens: int = 0,
        cache_read_input_tokens: int = 0,
    ) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_creation_input_tokens = cache_creation_input_tokens
        self.cache_read_input_tokens = cache_read_input_tokens


class MockContentBlock:
    def __init__(self, text: str = "Hello!") -> None:
        self.type = "text"
        self.text = text

    def model_dump(self) -> dict[str, str]:
        return {"type": self.type, "text": self.text}


class MockMessage:
    def __init__(
        self,
        *,
        model: str = "claude-sonnet-4-20250514",
        usage: MockUsage | None = None,
        content: list[MockContentBlock] | None = None,
        stop_reason: str = "end_turn",
        id: str = "msg_123",
    ) -> None:
        self.model = model
        self.usage = usage or MockUsage()
        self.content = content or [MockContentBlock()]
        self.stop_reason = stop_reason
        self.id = id


class MockStreamEvent:
    def __init__(self, event_type: str) -> None:
        self.type = event_type


class MockStreamContext:
    """Mock for Anthropic's MessageStream context manager."""

    def __init__(self, events: list[MockStreamEvent], final_message: MockMessage | None = None) -> None:
        self._events = events
        self._final_message = final_message or MockMessage()
        self._iter = iter(events)

    def __enter__(self) -> "MockStreamContext":
        return self

    def __exit__(self, *args: Any) -> None:
        pass

    def __iter__(self) -> "MockStreamContext":
        return self

    def __next__(self) -> MockStreamEvent:
        return next(self._iter)

    def get_final_message(self) -> MockMessage:
        return self._final_message


# --- Fixtures ---


@pytest.fixture(autouse=True)
def _clean_state() -> Any:
    _initialized.clear()
    _shutdown_in_progress.clear()
    set_current_span(None)
    yield
    if agentc.is_initialized():
        agentc.shutdown()
    _initialized.clear()
    _shutdown_in_progress.clear()
    set_current_span(None)
    import agentc._span as span_mod
    span_mod._logged_not_initialized = False


@pytest.fixture()
def tmp_storage(tmp_path: Path) -> Path:
    return tmp_path / "agentc"


@pytest.fixture()
def initialized(tmp_storage: Path) -> Path:
    # Don't apply patches during init — we test wrappers directly
    with patch("agentc._lifecycle._apply_patches"):
        agentc.init(storage_path=str(tmp_storage))
    return tmp_storage


# --- Tests ---


class TestExtractHelpers:
    def test_extract_request_attrs(self) -> None:
        kwargs = {
            "model": "claude-sonnet-4-20250514",
            "temperature": 0.7,
            "top_p": 0.9,
            "max_tokens": 1024,
        }
        attrs = _extract_request_attrs(kwargs)
        assert attrs["gen_ai.operation.name"] == "chat"
        assert attrs["gen_ai.provider.name"] == "anthropic"
        assert attrs["gen_ai.request.model"] == "claude-sonnet-4-20250514"
        assert attrs["gen_ai.request.temperature"] == 0.7
        assert attrs["gen_ai.request.top_p"] == 0.9
        assert attrs["gen_ai.request.max_tokens"] == 1024

    def test_extract_response_attrs(self) -> None:
        resp = MockMessage(
            model="claude-sonnet-4-20250514",
            usage=MockUsage(input_tokens=200, output_tokens=100),
            stop_reason="end_turn",
            id="msg_abc",
        )
        attrs = _extract_response_attrs(resp)
        assert attrs["gen_ai.response.model"] == "claude-sonnet-4-20250514"
        assert attrs["gen_ai.response.id"] == "msg_abc"
        assert attrs["gen_ai.response.finish_reasons"] == "end_turn"
        assert attrs["gen_ai.usage.input_tokens"] == 200
        assert attrs["gen_ai.usage.output_tokens"] == 100

    def test_extract_response_cache_tokens(self) -> None:
        resp = MockMessage(
            usage=MockUsage(cache_creation_input_tokens=50, cache_read_input_tokens=30),
        )
        attrs = _extract_response_attrs(resp)
        assert attrs["gen_ai.usage.cache_creation.input_tokens"] == 50
        assert attrs["gen_ai.usage.cache_read.input_tokens"] == 30

    def test_extract_input_messages(self) -> None:
        kwargs = {"messages": [{"role": "user", "content": "hello"}]}
        result = _extract_input_messages(kwargs)
        assert result is not None
        parsed = json.loads(result)
        assert parsed[0]["role"] == "user"
        assert parsed[0]["content"] == "hello"

    def test_extract_input_messages_none(self) -> None:
        assert _extract_input_messages({}) is None


class TestSyncCreateWrapper:
    """Test _wrap_create directly, bypassing wrapt mechanics."""

    def test_captures_span(self, initialized: Path) -> None:
        written: list[dict[str, Any]] = []
        mock_response = MockMessage()

        with patch("agentc._patches._anthropic._write_root_span", side_effect=lambda d: written.append(d)):
            wrapped = MagicMock(return_value=mock_response)
            result = _wrap_create(
                wrapped,
                None,
                (),
                {
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 1024,
                    "messages": [{"role": "user", "content": "hello"}],
                },
            )

        assert result is mock_response
        assert len(written) == 1
        span = written[0]
        assert span["kind"] == "chat"
        assert span["name"] == "anthropic.messages.create"
        assert span["status"] == "OK"
        attrs = json.loads(span["attributes"])
        assert attrs["gen_ai.operation.name"] == "chat"
        assert attrs["gen_ai.provider.name"] == "anthropic"
        assert attrs["gen_ai.request.model"] == "claude-sonnet-4-20250514"

    def test_captures_all_gen_ai_attrs(self, initialized: Path) -> None:
        written: list[dict[str, Any]] = []
        mock_response = MockMessage(
            model="claude-sonnet-4-20250514",
            usage=MockUsage(input_tokens=200, output_tokens=100, cache_creation_input_tokens=10, cache_read_input_tokens=5),
            stop_reason="end_turn",
            id="msg_xyz",
        )

        with patch("agentc._patches._anthropic._write_root_span", side_effect=lambda d: written.append(d)):
            wrapped = MagicMock(return_value=mock_response)
            _wrap_create(
                wrapped,
                None,
                (),
                {
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 1024,
                    "temperature": 0.5,
                    "top_p": 0.9,
                    "messages": [],
                },
            )

        attrs = json.loads(written[0]["attributes"])
        assert attrs["gen_ai.request.model"] == "claude-sonnet-4-20250514"
        assert attrs["gen_ai.request.max_tokens"] == 1024
        assert attrs["gen_ai.request.temperature"] == 0.5
        assert attrs["gen_ai.request.top_p"] == 0.9
        assert attrs["gen_ai.response.model"] == "claude-sonnet-4-20250514"
        assert attrs["gen_ai.response.id"] == "msg_xyz"
        assert attrs["gen_ai.response.finish_reasons"] == "end_turn"
        assert attrs["gen_ai.usage.input_tokens"] == 200
        assert attrs["gen_ai.usage.output_tokens"] == 100
        assert attrs["gen_ai.usage.cache_creation.input_tokens"] == 10
        assert attrs["gen_ai.usage.cache_read.input_tokens"] == 5

    def test_error_captures_error_span(self, initialized: Path) -> None:
        written: list[dict[str, Any]] = []

        with patch("agentc._patches._anthropic._write_root_span", side_effect=lambda d: written.append(d)):
            wrapped = MagicMock(side_effect=RuntimeError("API error"))
            with pytest.raises(RuntimeError, match="API error"):
                _wrap_create(
                    wrapped,
                    None,
                    (),
                    {
                        "model": "claude-sonnet-4-20250514",
                        "max_tokens": 1024,
                        "messages": [{"role": "user", "content": "hello"}],
                    },
                )

        assert len(written) == 1
        assert written[0]["status"] == "ERROR"
        attrs = json.loads(written[0]["attributes"])
        assert attrs["error.type"] == "RuntimeError"
        assert attrs["error.message"] == "API error"

    def test_noop_without_init(self) -> None:
        """Without init(), wrapper passes through unchanged."""
        mock_response = MockMessage()
        wrapped = MagicMock(return_value=mock_response)
        result = _wrap_create(
            wrapped,
            None,
            (),
            {"model": "test", "messages": []},
        )
        assert result is mock_response
        wrapped.assert_called_once()

    def test_input_messages_captured(self, initialized: Path) -> None:
        written: list[dict[str, Any]] = []

        with patch("agentc._patches._anthropic._write_root_span", side_effect=lambda d: written.append(d)):
            wrapped = MagicMock(return_value=MockMessage())
            _wrap_create(
                wrapped,
                None,
                (),
                {
                    "model": "test",
                    "max_tokens": 100,
                    "messages": [{"role": "user", "content": "hello world"}],
                },
            )

        span = written[0]
        assert "input_messages" in span
        parsed = json.loads(span["input_messages"])
        assert parsed[0]["content"] == "hello world"

    def test_output_messages_captured(self, initialized: Path) -> None:
        written: list[dict[str, Any]] = []

        with patch("agentc._patches._anthropic._write_root_span", side_effect=lambda d: written.append(d)):
            wrapped = MagicMock(return_value=MockMessage(content=[MockContentBlock("Hi there!")]))
            _wrap_create(wrapped, None, (), {"model": "test", "max_tokens": 100, "messages": []})

        span = written[0]
        assert "output_messages" in span
        parsed = json.loads(span["output_messages"])
        assert parsed[0]["role"] == "assistant"

    def test_timestamps_set(self, initialized: Path) -> None:
        written: list[dict[str, Any]] = []

        with patch("agentc._patches._anthropic._write_root_span", side_effect=lambda d: written.append(d)):
            wrapped = MagicMock(return_value=MockMessage())
            _wrap_create(wrapped, None, (), {"model": "test", "messages": []})

        s = written[0]
        assert s["start_time"] > 0
        assert s["end_time"] >= s["start_time"]


class TestSyncStreamWrapper:
    def test_stream_captures_ttft(self, initialized: Path) -> None:
        written: list[dict[str, Any]] = []

        with patch("agentc._patches._anthropic._write_root_span", side_effect=lambda d: written.append(d)):
            events = [
                MockStreamEvent("message_start"),
                MockStreamEvent("content_block_delta"),
                MockStreamEvent("content_block_delta"),
                MockStreamEvent("message_delta"),
            ]
            mock_stream = MockStreamContext(events, MockMessage())
            wrapped = MagicMock(return_value=mock_stream)

            stream_mgr = _wrap_stream(
                wrapped,
                None,
                (),
                {
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 1024,
                    "messages": [{"role": "user", "content": "hello"}],
                },
            )

            with stream_mgr as stream:
                for _ in stream:
                    pass

        assert len(written) == 1
        span = written[0]
        assert span["name"] == "anthropic.messages.stream"
        attrs = json.loads(span["attributes"])
        assert "agentc.ttft_ms" in attrs
        assert attrs["agentc.ttft_ms"] >= 0

    def test_noop_without_init(self) -> None:
        mock_stream = MockStreamContext([MockStreamEvent("text")], MockMessage())
        wrapped = MagicMock(return_value=mock_stream)
        result = _wrap_stream(wrapped, None, (), {"model": "test", "messages": []})
        # Should return the original stream
        assert result is mock_stream


class TestWithTraceContext:
    def test_span_inherits_trace(self, initialized: Path) -> None:
        """Anthropic span inherits trace_id from active @trace context."""
        written: list[dict[str, Any]] = []

        # Set up a fake parent trace context
        parent_ctx = SpanContext(span_id="parent123456789a", trace_id="trace12345678901234567890123456ab", name="my-agent")
        set_current_span(parent_ctx)

        with patch("agentc._patches._anthropic._write_root_span", side_effect=lambda d: written.append(d)):
            wrapped = MagicMock(return_value=MockMessage())
            _wrap_create(wrapped, None, (), {"model": "test", "messages": []})

        set_current_span(None)

        assert len(written) == 1
        span = written[0]
        assert span["trace_id"] == "trace12345678901234567890123456ab"
        assert span["parent_span_id"] == "parent123456789a"

    def test_agent_name_from_trace(self, initialized: Path) -> None:
        """gen_ai.agent.name extracted from active @trace context."""
        written: list[dict[str, Any]] = []

        parent_ctx = SpanContext(span_id="parent123456789a", trace_id="trace12345678901234567890123456ab", name="reviewer")
        set_current_span(parent_ctx)

        with patch("agentc._patches._anthropic._write_root_span", side_effect=lambda d: written.append(d)):
            wrapped = MagicMock(return_value=MockMessage())
            _wrap_create(wrapped, None, (), {"model": "test", "messages": []})

        set_current_span(None)

        attrs = json.loads(written[0]["attributes"])
        assert attrs["gen_ai.agent.name"] == "reviewer"

    def test_root_span_without_trace(self, initialized: Path) -> None:
        """Without active trace, creates root span with fresh trace_id."""
        written: list[dict[str, Any]] = []

        with patch("agentc._patches._anthropic._write_root_span", side_effect=lambda d: written.append(d)):
            wrapped = MagicMock(return_value=MockMessage())
            _wrap_create(wrapped, None, (), {"model": "test", "messages": []})

        span = written[0]
        assert "parent_span_id" not in span
        assert len(span["trace_id"]) == 32


class TestIntegration:
    def test_trace_with_create(self, initialized: Path) -> None:
        """Full flow: @trace → _wrap_create → spans linked correctly."""
        written: list[dict[str, Any]] = []

        with patch("agentc._patches._anthropic._write_root_span", side_effect=lambda d: written.append(d)):
            with patch("agentc._span._write_root_span", side_effect=lambda d: written.append(d)):

                @agentc.trace(name="my-agent")
                def agent() -> Any:
                    wrapped = MagicMock(return_value=MockMessage())
                    return _wrap_create(
                        wrapped,
                        None,
                        (),
                        {"model": "claude-sonnet-4-20250514", "max_tokens": 1024, "messages": [{"role": "user", "content": "hi"}]},
                    )

                result = agent()

        assert isinstance(result, MockMessage)
        agent_spans = [s for s in written if s["name"] == "my-agent"]
        api_spans = [s for s in written if s["name"] == "anthropic.messages.create"]
        assert len(agent_spans) == 1
        assert len(api_spans) == 1
        # Same trace
        assert api_spans[0]["trace_id"] == agent_spans[0]["trace_id"]
        # Parent chain
        assert api_spans[0]["parent_span_id"] == agent_spans[0]["span_id"]


class TestPatchUnpatch:
    def test_patch_applies_to_class(self) -> None:
        """patch() wraps the Anthropic Messages.create method."""
        import agentc._patches._anthropic as mod

        mod._patched = False
        patch_anthropic()
        try:
            import anthropic.resources.messages as msgs
            assert hasattr(msgs.Messages.create, "__wrapped__")
        finally:
            unpatch_anthropic()
            mod._patched = False

    def test_unpatch_restores(self) -> None:
        import agentc._patches._anthropic as mod

        mod._patched = False
        import anthropic.resources.messages as msgs

        original = msgs.Messages.create
        patch_anthropic()
        unpatch_anthropic()
        mod._patched = False
        # After unpatch, __wrapped__ should still be present but method should be the unwrapped one
        # wrapt's unwrap restores via __wrapped__
        current = msgs.Messages.create
        # Either restored to original or it's the unwrapped version
        assert not hasattr(current, "__wrapped__") or current is original

    def test_skip_if_not_installed(self) -> None:
        """If anthropic not installed, patch is skipped silently."""
        import agentc._patches._anthropic as mod

        mod._patched = False
        with patch.dict("sys.modules", {"anthropic": None}):
            with patch("builtins.__import__", side_effect=ImportError("no anthropic")):
                patch_anthropic()
        assert not mod._patched
