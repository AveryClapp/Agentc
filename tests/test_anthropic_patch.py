"""Tests for Anthropic SDK patches (bd-2up).

Run: maturin develop && pytest tests/test_anthropic_patch.py -v
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import agentc
from agentc._context import SpanContext, set_current_span
from agentc._lifecycle import _initialized, _shutdown_in_progress
from agentc._patches._anthropic import (
    _AsyncWrappedStreamManager,
    _extract_input_messages,
    _extract_request_attrs,
    _extract_response_attrs,
    _wrap_create,
    _wrap_create_async,
    _wrap_stream,
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

    def test_excluded_scope_preserves_request_and_span(self, initialized: Path) -> None:
        from agentc import optimization_scope

        written: list[dict[str, Any]] = []
        mock_response = MockMessage()
        request = {
            "model": "user-simulator-model",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "hello"}],
        }
        wrapped = MagicMock(return_value=mock_response)

        with optimization_scope("tau2.user_simulator", optimize=False), patch(
            "agentc._patches._anthropic._write_root_span",
            side_effect=lambda span: written.append(span),
        ), patch("agentc._optimizer.plan_call") as planner:
            result = _wrap_create(wrapped, None, (), request)

        assert result is mock_response
        planner.assert_not_called()
        wrapped.assert_called_once_with(**request)
        attrs = json.loads(written[0]["attributes"])
        assert attrs["agentc.optimization.scope"] == "tau2.user_simulator"
        assert attrs["agentc.optimization.eligible"] is False
        assert attrs["agentc.optimization.decision_reason"] == "scope_excluded"

    def test_native_anthropic_runs_shadow_guard(self, initialized: Path) -> None:
        # Regression (bd-1cb): the accuracy guard (maybe_shadow_record) must run
        # on the native Anthropic path, not only OpenAI. Previously it was
        # called from _openai.py only, so Anthropic users got no auto-disable.
        from agentc._optimizer import Plan

        shadow_calls: list[Any] = []
        plan = Plan(
            kind="rewritten",
            rule="ModelDowngrade",
            call={"model": "claude-x", "messages": []},
        )
        mock_response = MockMessage()

        with patch("agentc._patches._anthropic._write_root_span"), patch(
            "agentc._patches._anthropic._plan_anthropic_call", return_value=(plan, "site")
        ), patch("agentc._patches._anthropic._observe_anthropic_outcome"), patch(
            "agentc._patches._optimizer_glue.dispatch_sync", return_value=mock_response
        ), patch(
            "agentc._patches._optimizer_glue.maybe_shadow_record",
            side_effect=lambda *a, **k: shadow_calls.append(a),
        ):
            wrapped = MagicMock(return_value=mock_response)
            result = _wrap_create(
                wrapped,
                None,
                (),
                {"model": "claude-x", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]},
            )

        assert result is mock_response
        assert len(shadow_calls) == 1, "maybe_shadow_record must run on the native Anthropic path"

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


class TestAsyncCreateWrapper:
    @pytest.mark.asyncio
    async def test_native_anthropic_runs_async_shadow_guard(
        self, initialized: Path
    ) -> None:
        from agentc._optimizer import Plan

        plan = Plan(
            kind="rewritten",
            rule="OutputBudget",
            call={"model": "claude-sonnet-4-20250514", "messages": []},
        )
        response = MockMessage()
        shadow = AsyncMock()

        with patch("agentc._patches._anthropic._write_root_span"), patch(
            "agentc._patches._anthropic._plan_anthropic_call",
            return_value=(plan, "site"),
        ), patch(
            "agentc._executor.dispatch",
            new=AsyncMock(return_value=response),
        ), patch(
            "agentc._patches._optimizer_glue.maybe_shadow_record_async",
            new=shadow,
        ), patch("agentc._patches._anthropic._observe_anthropic_outcome"):
            result = await _wrap_create_async(
                AsyncMock(return_value=response),
                None,
                (),
                {
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 128,
                    "messages": [],
                },
            )

        assert result is response
        shadow.assert_awaited_once()
        assert shadow.await_args.args[:3] == (plan, "site", response)


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

    def test_stream_exit_does_not_mask_user_exception(self, initialized: Path) -> None:
        # Regression (MNT-058, P7-1): if the user's with-body raises AND
        # get_final_message would raise (stream closed / until_done bare
        # assert), Agentc must not read it and must not mask the user's
        # original exception. Its exception is the only thing the caller sees.
        class ExplodingFinal(MockStreamContext):
            def get_final_message(self) -> MockMessage:
                raise AssertionError("until_done() bare assert would mask the user error")

        with patch("agentc._patches._anthropic._write_root_span", side_effect=lambda d: None):
            mock_stream = ExplodingFinal([MockStreamEvent("text")], MockMessage())
            wrapped = MagicMock(return_value=mock_stream)
            stream_mgr = _wrap_stream(wrapped, None, (), {"model": "m", "messages": []})
            with pytest.raises(ValueError, match="user error"):
                with stream_mgr as stream:  # noqa: F841
                    raise ValueError("user error")


class TestAsyncStreamWrapper:
    @pytest.mark.asyncio
    async def test_async_stream_awaits_get_final_message(self) -> None:
        # Regression (MNT-... , P7-2): get_final_message is a coroutine in the
        # async SDK. The old code never awaited it, so final_message was a
        # coroutine object (not None) and _extract_response_attrs saw {} —
        # every async streaming span lost its response attributes.
        captured: dict[str, Any] = {}

        class AsyncFinal:
            usage = MockUsage()
            model = "claude-sonnet-4-20250514"
            stop_reason = "end_turn"

            async def get_final_message(self) -> "AsyncFinal":
                return self

        class AsyncMgr:
            async def __aexit__(self, *exc: Any) -> bool:
                return False

        mgr = _AsyncWrappedStreamManager.__new__(_AsyncWrappedStreamManager)
        mgr._stream_mgr = AsyncMgr()
        mgr._stream = AsyncFinal()
        mgr._start_time = 0
        mgr._req_attrs = {}
        mgr._input_msgs = None
        mgr._parent = None

        with patch(
            "agentc._patches._anthropic._emit_span",
            side_effect=lambda **kw: captured.update(kw),
        ):
            await mgr.__aexit__(None, None, None)

        # Response attrs came from the resolved message — an un-awaited
        # coroutine has no `.model`, so this would be absent on the old code.
        assert mgr._req_attrs.get("gen_ai.response.model") == "claude-sonnet-4-20250514"
        assert captured.get("attrs", {}).get("gen_ai.response.model") == "claude-sonnet-4-20250514"


class TestWithTraceContext:
    def test_span_inherits_trace(self, initialized: Path) -> None:
        """Anthropic span inherits trace_id from active @trace context."""
        written: list[dict[str, Any]] = []

        # Set up a fake parent trace context
        parent_ctx = SpanContext(span_id="parent123456789a", trace_id="trace12345678901234567890123456ab", name="my-agent")
        set_current_span(parent_ctx)

        # Non-root spans now route through ``_enqueue_span`` (bd-4hy);
        # mock both so the test pins span content regardless of route.
        with patch("agentc._patches._anthropic._write_root_span", side_effect=lambda d: written.append(d)), \
             patch("agentc._patches._anthropic._enqueue_span", side_effect=lambda d: written.append(d)):
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

        with patch("agentc._patches._anthropic._write_root_span", side_effect=lambda d: written.append(d)), \
             patch("agentc._patches._anthropic._enqueue_span", side_effect=lambda d: written.append(d)):
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

        with patch("agentc._patches._anthropic._write_root_span", side_effect=lambda d: written.append(d)), \
             patch("agentc._patches._anthropic._enqueue_span", side_effect=lambda d: written.append(d)), \
             patch("agentc._span._write_root_span", side_effect=lambda d: written.append(d)), \
             patch("agentc._span._enqueue_span", side_effect=lambda d: written.append(d)):

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
        pytest.importorskip("anthropic")
        import agentc._patches._anthropic as mod

        mod._patched = False
        patch_anthropic()
        try:
            import anthropic.resources.messages as msgs

            assert hasattr(msgs.Messages.create, "__wrapped__")
        finally:
            unpatch_anthropic()
            mod._patched = False

    def test_patch_applies_to_beta_class_used_by_osworld(self) -> None:
        """patch() wraps ``client.beta.messages`` as used by OSWorld V2."""
        pytest.importorskip("anthropic")
        beta_msgs = pytest.importorskip("anthropic.resources.beta.messages")
        import agentc._patches._anthropic as mod

        mod._patched = False
        patch_anthropic()
        try:
            assert hasattr(beta_msgs.Messages.create, "__wrapped__")
            assert hasattr(beta_msgs.AsyncMessages.create, "__wrapped__")
            assert hasattr(beta_msgs.Messages.stream, "__wrapped__")
            assert hasattr(beta_msgs.AsyncMessages.stream, "__wrapped__")
        finally:
            unpatch_anthropic()
            mod._patched = False

    def test_unpatch_restores(self) -> None:
        pytest.importorskip("anthropic")
        import agentc._patches._anthropic as mod

        mod._patched = False
        import anthropic.resources.messages as msgs

        beta_msgs = pytest.importorskip("anthropic.resources.beta.messages")

        original = msgs.Messages.create
        beta_original = beta_msgs.Messages.create
        patch_anthropic()
        unpatch_anthropic()
        mod._patched = False
        # After unpatch, __wrapped__ should still be present but method should be the unwrapped one
        # wrapt's unwrap restores via __wrapped__
        current = msgs.Messages.create
        # Either restored to original or it's the unwrapped version
        assert not hasattr(current, "__wrapped__") or current is original
        assert beta_msgs.Messages.create is beta_original

    def test_skip_if_not_installed(self) -> None:
        """If anthropic not installed, patch is skipped silently."""
        import agentc._patches._anthropic as mod

        mod._patched = False
        with patch.dict("sys.modules", {"anthropic": None}):
            with patch("builtins.__import__", side_effect=ImportError("no anthropic")):
                patch_anthropic()
        assert not mod._patched
